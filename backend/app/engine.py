"""The trading engine: one background thread that runs the trading day by itself.

Every CYCLE_SEC seconds, on trading days:
  09:00-15:45  log in once per IST day (smart.Session: reuse, token refresh, re-login),
               keep the skill pack's live feed (in-common/feed.py, the only WebSocket)
               running and restart it with backoff if it dies, subscribe the indices and
               every open trade's option, read prices from the feed's tick store
               (REST quote fallback when a tick is stale), manage open paper trades
               (premium/underlying stop and target, flat-by time).
  09:15-15:30  on every closed 5-minute bar, run both strategies on each index; record
               every new signal; trade it on paper only if the backend gate passes
               (entry cutoff 14:00 IST, limits, no conflicting position, fresh signal).
Outside those hours it idles. Stop ends the thread and the feed; open trades stay
OPEN in the database and are managed again after Start. It never places a real
order: the skill pack has no order function at all.
"""

import argparse
import contextlib
import datetime
import io
import json
import logging
import os
import subprocess
import sys
import threading
import time

from app import config, paper, research, skills, timemachine
from app import strategies as strat
from app.sp import FEED_SCRIPT, chain, exitplan, firstrun, risk, size, smart, tm
from app.store import Store

log = logging.getLogger("engine")
CYCLE_SEC = 1             # position checks are local SQLite reads: cheap, and expiry-day premiums move fast
LIVE_TICK_SEC = 15          # an older feed tick is not used for exits: REST quote instead
QUOTE_EVERY_SEC = 5
BAR_RETRY_SEC = 10
BAR_WAIT_SEC = 120          # evaluate anyway when the newest bar is this late
LOGIN_RETRY_SEC = 60
ALERTS_EVERY_SEC = 60
OPTIONS_NOTE = ("No historical option prices: Angel One's getCandleData serves contracts that still trade, not "
                "expired weeklies, and in-chain's own snapshots (trader/history/chain/) cover too few days. Results "
                "here are index moves in R with option costs estimated in R; they are not option P&L.")
FRESH_WAIT_SEC = 5          # in-risk's stale-data check: wait this long for fresh option and index ticks
DEFAULT_SYMBOLS = ["NIFTY", "SENSEX", "BANKNIFTY"]
SKILL_IO = threading.Lock()  # contextlib.redirect_stdout is process-wide: one in-process skill call at a time


class EngineError(Exception):
    pass


def hm_to_min(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def last_closed_bar(now):
    """Start (HH:MM) of the newest 5-minute bar closed by now, 09:15..15:25, or None before 09:20."""
    m = (now.hour * 60 + now.minute) - 555
    idx = min(m // 5 - 1, 74)          # 74 = the 15:25 bar, the last of the day
    return None if idx < 0 else "%02d:%02d" % divmod(555 + 5 * idx, 60)


def phase(now, cal):
    if not smart.trading_day(now.date(), cal):
        holiday = (cal.get("holidays") or {}).get(now.date().isoformat())
        return "closed", "holiday (%s)" % holiday if holiday else "weekend"
    hm = now.strftime("%H:%M")
    for until, name, why in (("09:00", "closed", "before 09:00 IST"), ("09:15", "pre_open", "pre-open"),
                             ("15:30", "open", "market open"), ("15:45", "post_close", "after the close")):
        if hm < until:
            return name, why
    return "closed", "after 15:45 IST"


class InstanceLock:
    """An OS file lock: one engine per trader folder, released when the process dies."""

    def __init__(self, path):
        self.path, self.fh = path, None

    def acquire(self):
        if self.fh:
            return True
        fh = open(self.path, "a")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self.fh = fh
        return True


class FeedSupervisor:
    """Keeps in-common/feed.py running. feed_status.json younger than 15 s means a feed is alive
    (ours or one started by hand); feed.py itself refuses a second feed per folder."""

    def __init__(self, trader):
        self.trader, self.proc, self.next_try, self.backoff = trader, None, 0.0, 5.0
        self.status_path = os.path.join(trader, "state", "feed_status.json")
        self.log_path = os.path.join(trader, "state", "feed.log")
        self.pid_path = os.path.join(trader, "state", "feed.pid")
        self.restarts = 0

    def status(self):
        try:
            st = smart.read_json(self.status_path, {}) or {}
            self.last = st
        except (OSError, ValueError):   # Windows: the feed is replacing the file this instant; use the last read
            st = getattr(self, "last", {})
        alive = bool(st.get("running")) and time.time() * 1000 - (st.get("updated_ms") or 0) < 15000
        return dict(st, alive=alive, managed=self.proc is not None and self.proc.poll() is None,
                    restarts=self.restarts)

    def ensure(self):
        if self.status()["alive"]:
            self.backoff = 5.0
            return
        if self.proc is not None and self.proc.poll() is None:
            return                                          # starting up
        if time.time() < self.next_try:
            return
        if self.proc is not None:
            self.restarts += 1
            log.warning("feed exited (code %s); restarting, see %s", self.proc.returncode, self.log_path)
        logf = open(self.log_path, "a", encoding="utf-8")
        logf.write("\n--- feed start %s ---\n" % smart.now_ist().isoformat())
        logf.flush()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # the base interpreter, not a venv launcher stub, so proc.pid is the feed's own pid (feed.py is stdlib only)
        python = getattr(sys, "_base_executable", None) or sys.executable
        self.proc = subprocess.Popen([python, FEED_SCRIPT, "run", "--trader", self.trader],
                                     cwd=str(config.BACKEND_DIR), stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=flags)
        logf.close()
        with open(self.pid_path, "w") as fh:
            fh.write(str(self.proc.pid))
        log.info("feed started (pid %s)", self.proc.pid)
        self.next_try = time.time() + self.backoff
        self.backoff = min(self.backoff * 2, 60.0)

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            log.info("feed stopped")
        elif self.proc is None:
            # a feed this engine started before a crash: feed_status still names our pid
            st = self.status()
            mine = (smart.read_json(self.pid_path) if os.path.exists(self.pid_path) else None)
            if st["alive"] and mine and st.get("pid") == mine:
                try:
                    os.kill(int(mine), 15)
                    log.info("stopped the feed left by an earlier backend (pid %s)", mine)
                except OSError as ex:
                    log.warning("could not stop feed pid %s: %s", mine, ex)
        self.proc = None


class Engine:
    def __init__(self, trader=config.TRADER_DIR):
        self.trader = trader
        firstrun.ensure(firstrun.trader_paths(trader))      # folders + config/calendar/charges templates
        self.p = smart.trader_paths(trader)
        self.store = Store(os.path.join(self.p["state"], "paper.sqlite"))
        self.instance = InstanceLock(os.path.join(self.p["state"], "engine.lock"))
        self.feed = FeedSupervisor(trader)
        self.thread, self.stop_event, self.op = None, threading.Event(), threading.RLock()
        self.listeners = []
        self.client, self.client_day, self.next_login = None, None, 0.0
        self.day, self.facts, self.prev_bars, self.prev_close = None, {}, {}, {}
        self.market, self.last_eval, self.next_eval, self.next_quote, self.quote_cache = {}, {}, {}, {}, {}
        self.subscribed = None
        self.tm_window, self.tm_cache, self.master_rows, self.bars = None, {}, None, {}
        self.background, self.news_day, self.next_alerts = skills.Background(), None, 0.0         # in-timemachine: the rolling window store, verdicts by day
        self.status = {"phase": None, "phase_reason": None, "last_cycle_ist": None, "last_error": None,
                       "errors": 0, "started_at": None, "cycles": 0}
        self.angel = {"logged_in": False, "session_date": None, "last_ok_ist": None, "last_error": None}

    # ------------------------------------------------------------ lifecycle
    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()

    def start(self):
        if self.running:
            return
        if not self.instance.acquire():
            raise EngineError("another engine instance already runs on %s (one per trader folder)" % self.trader)
        if self.thread is not None and self.thread.is_alive():
            raise EngineError("the previous engine thread is still stopping; try again in a few seconds")
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="engine", daemon=True)
        self.thread.start()
        self.store.put("desired_running", "1")
        self.status["started_at"] = smart.now_ist().isoformat()
        log.info("engine started")
        self.emit("engine", {"running": True})

    def stop(self, remember=True):
        if self.instance.fh is None:
            # this process never held the lock: the engine, its feed and subscriptions belong to another one
            if remember:
                raise EngineError("this backend does not run the engine: another instance holds %s" % self.trader)
            return
        if remember:
            self.store.put("desired_running", "0")
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(60)
            if self.thread.is_alive():
                raise EngineError("the engine did not stop within 60 s (a broker call is still in flight)")
        self.feed.stop()
        try:
            smart.subscribe(self.trader, "engine", [])
        except OSError as e:
            log.warning("could not clear subscriptions: %s", e)
        self.subscribed = None
        log.info("engine stopped")
        self.emit("engine", {"running": False})

    def _run(self):
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                with self.op:
                    self.cycle(smart.now_ist())
                self.status["last_error"] = None
            except Exception as e:                      # one bad cycle never ends the day
                log.exception("cycle failed")
                self.status["last_error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
                self.status["errors"] += 1
            self.status["last_cycle_ist"] = smart.now_ist().isoformat()
            self.status["cycles"] += 1
            self.stop_event.wait(max(0.5, CYCLE_SEC - (time.time() - t0)))

    def emit(self, event, data):
        for fn in list(self.listeners):
            try:
                fn(event, data)
            except Exception:
                log.exception("listener failed")

    # ------------------------------------------------------------ the cycle
    def cycle(self, now):
        cfg = smart.load_config(self.trader)
        cal = smart.read_json(self.p["calendar"], {}) or {}
        ph, why = phase(now, cal)
        self.status.update(phase=ph, phase_reason=why)
        self.close_old_trades(now, cfg)
        if ph == "closed":
            self.feed.stop()
            return
        if not self.ensure_client(now):
            return
        self.ensure_day(now, cfg, cal)
        self.feed.ensure()
        self.update_subscriptions(cfg)
        self.update_market(now, cfg)
        self.monitor(now, cfg)
        self.scheduled_skills(now, cfg)
        if ph == "open":
            self.scan(now, cfg)

    def scheduled_skills(self, now, cfg):
        """in-news once a trading day from the pre-open (news.json: events, ban list, flags), in-alerts every
        ALERTS_EVERY_SEC while trades filled by hand with in-ticket are open. Each in its own thread."""
        auto = cfg.get("skills") or {}
        day = now.date().isoformat()
        if auto.get("news_daily", True) and self.news_day != day:
            if self.background.start("in-news", "run", None, self.trader):
                self.news_day = day
        if auto.get("alerts", True) and time.time() >= self.next_alerts and now.strftime("%H:%M") >= "09:15":
            self.next_alerts = time.time() + ALERTS_EVERY_SEC
            journal = (smart.read_json(self.p["journal"]) or {}).get("trades", [])
            if any(t.get("date") == day and t.get("fill_price") is not None and t.get("exit_price") is None
                   for t in journal):
                self.background.start("in-alerts", "check", None, self.trader)

    def symbols(self, cfg):
        syms = [s.upper() for s in ((cfg.get("paper") or {}).get("symbols") or DEFAULT_SYMBOLS)]
        return [s for s in syms if s in DEFAULT_SYMBOLS]

    def ensure_client(self, now):
        day = now.date().isoformat()
        if self.client is not None and self.client_day == day:
            return True
        if time.time() < self.next_login:
            return False
        try:
            self.client, self.limiter = smart.open_client(self.trader, now)
        except (smart.SmartError, OSError) as e:
            self.client, self.next_login = None, time.time() + LOGIN_RETRY_SEC
            self.angel.update(logged_in=False, last_error=str(e)[:300])
            log.error("Angel One login failed: %s (retry in %d s)", e, LOGIN_RETRY_SEC)
            return False
        self.client_day = day
        self.angel.update(logged_in=True, session_date=day, last_ok_ist=now.isoformat(), last_error=None)
        log.info("Angel One session ready for %s", day)
        return True

    def ensure_day(self, now, cfg, cal):
        day = now.date().isoformat()
        if self.day == day and all(s in self.facts for s in self.symbols(cfg)):
            return
        rows = smart.load_master(self.p["cache"], day)
        scale = smart.strike_scale(rows)[0]
        facts = {}
        for s in self.symbols(cfg):
            f = smart.underlying_facts(rows, s, now.date(), scale)
            if not f.get("index_token"):
                log.error("%s has no index row in today's instrument master", s)
                continue
            exps = f.get("expiries") or []
            facts[s] = {"token": str(f["index_token"]), "segment": f["index_segment"],
                        "expiry_today": bool(exps) and exps[0][0] == day,
                        "expiries": sorted({str(smart.expiry_date(r.get("expiry"))) for r in rows
                                            if r.get("instrumenttype") == "OPTIDX" and r.get("name") == s
                                            and smart.expiry_date(r.get("expiry"))})}
        names = set(facts)
        # what in-risk needs from the master: today's index options and the indices themselves
        self.master_rows = [r for r in rows if r.get("name") in names and r.get("instrumenttype") in ("OPTIDX", "AMXIDX")]
        del rows
        if self.day != day:
            self.prev_bars, self.prev_close, self.last_eval, self.next_eval = {}, {}, {}, {}
            self.tm_cache, self.bars = {}, {}
        self.day, self.facts = day, facts
        log.info("instrument master loaded for %s: %s", day,
                 ", ".join("%s token %s" % (s, f["token"]) for s, f in facts.items()))
        if timemachine.settings(cfg)["enabled"]:
            for s in facts:                              # roll the 60-day windows now, not at the first signal
                self.timemachine_window(s, now, cfg, cal)

    def update_subscriptions(self, cfg):
        items = [{"segment": self.facts[s]["segment"], "token": self.facts[s]["token"], "mode": "quote"}
                 for s in self.symbols(cfg) if s in self.facts]
        items += [{"segment": t["segment"], "token": t["token"], "mode": "quote"} for t in self.store.open_trades()]
        key = sorted((i["segment"], i["token"]) for i in items)
        if key != self.subscribed:
            smart.subscribe(self.trader, "engine", items)
            self.subscribed = key

    # ------------------------------------------------------------ prices
    def prices(self, tokens_by_seg, now):
        """{token: (ltp, source, age_sec)} from the feed, REST quote LTP for tokens with no live tick."""
        now_ms = int(time.time() * 1000)
        tokens = [t for ts in tokens_by_seg.values() for t in ts]
        latest = smart.read_latest(self.p["db"], tokens, now_ms, LIVE_TICK_SEC * 1000)
        out, need = {}, {}
        for seg, ts in tokens_by_seg.items():
            for t in ts:
                x = latest.get(t)
                ltp = paper.valid_price(x["ltp"]) if x else None
                if ltp is not None and not x["stale"]:
                    out[t] = (ltp, "feed", max(0.0, x["age_ms"] / 1000.0))
                elif time.time() >= self.next_quote.get(t, 0):
                    need.setdefault(seg, []).append(t)
                elif t in self.quote_cache:
                    out[t] = self.quote_cache[t]
        if need and self.client is not None:
            for t in (t for ts in need.values() for t in ts):
                self.next_quote[t] = time.time() + QUOTE_EVERY_SEC
            r = self.client.call("quote", {"mode": "LTP", "exchangeTokens": need})
            if r.ok:
                self.angel.update(last_ok_ist=now.isoformat(), last_error=None)
                cache = self.quote_cache
                for f in (r.data or {}).get("fetched") or []:
                    ltp = paper.valid_price(f.get("ltp"))
                    if ltp is not None:
                        out[str(f.get("symbolToken"))] = cache[str(f.get("symbolToken"))] = (ltp, "quote", 0.0)
            else:
                self.angel["last_error"] = "quote: %s" % r.why().strip()[:200]
                log.warning("quote fallback failed: %s", r.why().strip())
        return out

    def update_market(self, now, cfg):
        syms = [s for s in self.symbols(cfg) if s in self.facts]
        by_seg = {}
        for s in syms:
            by_seg.setdefault(self.facts[s]["segment"], []).append(self.facts[s]["token"])
        px = self.prices(by_seg, now)
        stale_sec = float((cfg.get("freshness") or {}).get("stale_tick_sec", 3))
        for s in syms:
            ltp, source, age = px.get(self.facts[s]["token"], (None, None, None))
            # every key exists from the start: the UI thread copies this dict while this thread updates it
            m = self.market.setdefault(s, {"symbol": s, "bars_source": None, "bars": 0, "last_bar_ist": None,
                                           "bars_note": None, "hybrid": None})
            prev = self.prev_close.get(s)
            m.update(ltp=ltp, source=source, age_sec=round(age, 1) if age is not None else None,
                     stale=ltp is None or (source == "feed" and age > stale_sec), prev_close=prev,
                     change=round(ltp - prev, 2) if ltp and prev else None,
                     change_pct=round((ltp / prev - 1) * 100, 2) if ltp and prev else None,
                     expiry_today=self.facts[s]["expiry_today"], as_of_ist=now.isoformat())

    # ------------------------------------------------------------ positions
    def monitor(self, now, cfg):
        trades = self.store.open_trades()
        if not trades:
            return
        by_seg = {}
        for t in trades:
            by_seg.setdefault(t["segment"], []).append(t["token"])
        px = self.prices(by_seg, now)
        hm = now.strftime("%H:%M")
        for t in trades:
            opt = px.get(t["token"], (None,))[0]
            und = (self.market.get(t["symbol"]) or {}).get("ltp")
            self.store.mark(t["id"], opt, now.isoformat() if opt else None, und)
            reason = paper.exit_reason(t, opt, und, hm)
            if not reason and paper.no_progress(t, self.bars.get(t["symbol"]), now):
                reason = "NO_PROGRESS"
            if not reason and hm >= "15:30":
                reason = "TIME_EXIT"
            if not reason:
                continue
            price = opt if opt is not None else (paper.valid_price(t.get("ltp")) if hm >= "15:30" else None)
            if price is None:
                log.warning("%s %s exit due (%s) but no valid option price yet", t["id"], t["tradingsymbol"], reason)
                continue
            self.close(t, price, reason, now)

    def close(self, t, price, reason, now):
        ch = smart.read_json(self.p["charges"], {}) or {}
        gross, charges, net = paper.pnl(t["entry_price"], price, t["order_qtys"], t["segment"], ch)
        if self.store.close_trade(t["id"], price, now.isoformat(), reason, round(gross, 2), round(charges, 2),
                                  round(net, 2)):
            log.info("PAPER EXIT %s %s %s @ %.2f (%s) net %.2f", t["id"], t["strategy"], t["tradingsymbol"], price,
                     reason, net)
            self.emit("trade_closed", self.store.trade(t["id"]))

    def close_old_trades(self, now, cfg):
        """A trade left OPEN from an earlier day (the backend was down at the close) is closed at its last price."""
        today = now.date().isoformat()
        for t in self.store.open_trades():
            if t["date"] < today:
                self.close(t, paper.valid_price(t.get("ltp")) or t["entry_price"], "SESSION_END_UNMONITORED", now)

    def close_manual(self, tid):
        with self.op:
            t = self.store.trade(tid)
            if not t or t["status"] != "OPEN":
                raise EngineError("trade %s is not open" % tid)
            now = smart.now_ist()
            px = self.prices({t["segment"]: [t["token"]]}, now) if self.client is not None else {}
            price = px.get(t["token"], (None,))[0]
            if price is None:
                raise EngineError("no live price for %s: cannot close it at a real price now" % t["tradingsymbol"])
            self.close(t, price, "MANUAL", now)
            return self.store.trade(tid)

    # ------------------------------------------------------------ signals
    def scan(self, now, cfg):
        expected = last_closed_bar(now)
        if expected is None:
            return
        for s in [s for s in self.symbols(cfg) if s in self.facts]:
            if self.last_eval.get(s) == expected or time.time() < self.next_eval.get(s, 0):
                continue
            f = self.facts[s]
            d = smart.day_bars(self.trader, f["segment"], f["token"], 5, now, self.client)
            bars = strat.clean_bars(d["bars"])
            self.bars[s] = bars                             # for in-exit's no-progress exit in monitor()
            m = self.market.setdefault(s, {"symbol": s})
            m.update(bars_source=d["source"], bars=len(bars), last_bar_ist=bars[-1]["start_ist"] if bars else None,
                     bars_note=d.get("partial"))
            late = (now - smart.parse_ist("%s %s" % (self.day, expected))).total_seconds() - 300
            if (not bars or bars[-1]["start_ist"] < expected) and late < BAR_WAIT_SEC:
                self.next_eval[s] = time.time() + BAR_RETRY_SEC
                continue
            prev = self.previous_day_bars(s, now)
            m["hybrid"] = self.hybrid_state(s, prev, bars, cfg)
            for sig in strat.evaluate(cfg, s, self.day, prev, bars, (cfg.get("session") or {}).get("range_end", "09:30")):
                if not self.store.has_signal(sig["id"]):
                    self.process(sig, cfg)
            if prev is None:                                # EMA_PULLBACK waits for the previous day's bars
                self.next_eval[s] = time.time() + BAR_RETRY_SEC
            else:
                self.last_eval[s] = expected
            now = smart.now_ist()                           # candle calls take time: keep positions managed
            self.update_market(now, cfg)
            self.monitor(now, cfg)

    def hybrid_state(self, sym, prev, bars, cfg):
        """EMA_ORB's live view for the dashboard, from the same strat.ema_orb_evaluate the signals come from:
        the opening range, the breakout status, the EMA trend and the last setups with their reasons."""
        s = strat.strategy_cfg(cfg, "EMA_ORB")
        range_end = (cfg.get("session") or {}).get("range_end", "09:30")
        _, st = strat.ema_orb_evaluate(sym, self.day, strat.clean_bars(prev or []), bars, s, range_end)
        rng, bo = st["opening_range"], st["breakout"]
        out = {"enabled": bool(s["enabled"]), "opening_range": rng, "min_or_pct": float(s["min_or_pct"]),
               "range_ok": bool(rng and rng["complete"] and rng["pct"] >= float(s["min_or_pct"])),
               "breakout": "above the high since %s" % bo["bullish"] if bo["bullish"] and (
                   not bo["bearish"] or bo["bullish"] > bo["bearish"]) else
               "below the low since %s" % bo["bearish"] if bo["bearish"] else "inside the range" if rng else None,
               "ema": None, "setups": st["setups"][-6:], "prev_day": prev is not None}
        allb = strat.clean_bars(prev or []) + list(bars)
        if len(allb) > int(s["slow"]) + 3:
            closes = [b["close"] for b in allb]
            f, sl = strat.ema(closes, int(s["fast"])), strat.ema(closes, int(s["slow"]))
            trend = "bullish" if f[-1] > sl[-1] and sl[-1] > sl[-4] else "bearish" if f[-1] < sl[-1] and \
                sl[-1] < sl[-4] else "no trend"
            out["ema"] = {"fast": round(f[-1], 2), "slow": round(sl[-1], 2), "trend": trend,
                          "period": [int(s["fast"]), int(s["slow"])]}
        return out

    def previous_day_bars(self, s, now):
        if s not in self.prev_bars:
            cal = smart.read_json(self.p["calendar"], {}) or {}
            prev = smart.previous_trading_day(now.date(), cal)
            f = self.facts[s]
            days, partial = smart.candles(self.client, self.p["cache"], f["segment"], f["token"], "FIVE_MINUTE",
                                          prev, prev, now)
            rows = days.get(prev.isoformat()) or []
            if partial and not rows:
                log.warning("%s previous-day bars unavailable (%s): EMA_PULLBACK retries in %d s", s, partial,
                            BAR_RETRY_SEC)
                return None
            self.prev_bars[s] = [{"start_ist": str(r[0])[11:16], "open": r[1], "high": r[2], "low": r[3],
                                  "close": r[4]} for r in rows]
            self.prev_close[s] = rows[-1][4] if rows else None
        return self.prev_bars[s]

    def process(self, sig, cfg):
        now = smart.now_ist()
        s = strat.strategy_cfg(cfg, sig["strategy"])
        today = now.date().isoformat()
        closed = [t for t in self.store.trades(today, "CLOSED") if t["exit_time"]]
        closed.sort(key=lambda t: t["exit_time"])
        streak = 0
        for t in reversed(closed):
            if (t["net_pnl"] or 0) > 0:
                break
            streak += 1
        mine = sorted((t for t in self.store.trades(today) if t["strategy"] == sig["strategy"]),
                      key=lambda t: t["entry_time"])
        ctx = {"open_trades": self.store.open_trades(), "trades_today": self.store.count_trades(today),
               "strategy_first_exit": mine[0]["exit_reason"] if mine else None,
               "strategy_trades_today": self.store.count_trades(today, sig["strategy"]),
               "realized_today": sum(t["net_pnl"] or 0 for t in closed), "losses_in_a_row": streak,
               "underlying_ltp": (self.market.get(sig["symbol"]) or {}).get("ltp"),
               "auto_execute": (cfg.get("paper") or {}).get("auto_execute", True), "s": s}
        status, reason = paper.gate(sig, now, cfg, ctx)
        meta = None
        if status in ("EXECUTE", "EXECUTABLE") and timemachine.settings(cfg)["enabled"] and \
                not self.store.has_signal(sig["id"]):
            doc = self.timemachine_verdict(sig["symbol"], sig["strategy"], now, cfg)
            log.info("TIME MACHINE %s %s: %s", "BLOCK" if not doc["ok"] else "PASS" if doc["data"] else "SKIP", sig["id"],
                     doc["reason"])
            sig["timemachine"] = timemachine.summary(doc)
            meta = {"timemachine": sig["timemachine"]}
            if not doc["ok"]:
                status, reason = "REJECTED", "time machine: %s" % doc["reason"]
        if not self.store.add_signal(sig, "EXECUTABLE" if status == "EXECUTE" else status,
                                     "opening the paper trade" if status == "EXECUTE" else reason, now.isoformat(),
                                     meta):
            return
        log.info("SIGNAL %s %s %s %s @ %.2f: %s%s", sig["strategy"], sig["symbol"], sig["direction"],
                 sig["bar_close_ist"], sig["entry_underlying"], status, " (%s)" % reason if reason else "")
        if status == "EXECUTE":
            try:
                self.execute(sig, now, cfg, s)
            except Exception as e:
                log.exception("paper entry failed for %s", sig["id"])
                self.store.set_signal(sig["id"], "REJECTED", "entry failed: %s" % str(e)[:250])
        self.emit("signal", next((x for x in self.store.signals(today, 50) if x["id"] == sig["id"]), sig))

    def execute(self, sig, now, cfg, s):
        """The skill pack's pipeline for one signal, before any paper trade opens:
        in-chain pick -> in-exit plan -> in-budget size -> in-timemachine (after charges) -> in-risk PASS/BLOCK."""
        sym, side = sig["symbol"], sig["direction"]
        a = argparse.Namespace(trader=self.trader, underlying=sym, direction=side, now=None, offline=None, cmd="pick")
        out = io.StringIO()
        try:
            with SKILL_IO, contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = chain.cmd_pick(a)
        except smart.SmartError as e:
            rc, out = 1, io.StringIO(str(e))
        text = out.getvalue().strip().splitlines()
        try:
            self._execute_picked(sig, now, cfg, s, rc, text)
        finally:
            smart.subscribe(self.trader, "in-chain:" + sym, [])  # the pick's chain tokens: kept until in-risk ran
            smart.subscribe(self.trader, "engine:pending", [])   # an opened trade is in the engine's own list now

    def _reject(self, sig, reason, meta=None):
        self.store.set_signal(sig["id"], "REJECTED", reason[:600], meta=meta)
        log.info("REJECTED %s: %s", sig["id"], reason)

    def _execute_picked(self, sig, now, cfg, s, rc, text):
        sym, side = sig["symbol"], sig["direction"]
        meta = {"timemachine": sig.get("timemachine")} if sig.get("timemachine") else {}
        if rc != 0:
            return self._reject(sig, "no option passed the pick rules: %s" % (text[-1] if text else "in-chain refused"),
                                meta or None)
        pick = smart.read_json(self.p["pick"]) or {}
        inst = pick.get("instrument") or {}
        if pick.get("underlying") != sym or pick.get("direction") != side or \
                (now - smart.parse_ist(pick["as_of_ist"])).total_seconds() > 60:
            return self._reject(sig, "pick.json does not match this signal", meta or None)
        entry = paper.valid_price(pick.get("ask")) or paper.valid_price(pick.get("premium"))
        if entry is None:
            return self._reject(sig, "no valid option price for %s" % inst.get("tradingsymbol"), meta or None)
        meta.update(tradingsymbol=inst.get("tradingsymbol"),
                    pick={k: pick.get(k) for k in ("premium", "bid", "ask", "spread_pct", "delta", "volume", "oi",
                                                   "greeks_source", "one_lot_cost_inr")})

        # in-exit: the plan (premium stop and target, flat-by, no-progress), from the engine's own entry.json
        try:
            plan = self.exit_plan(sig, s, entry, cfg, now)
        except smart.SmartError as e:
            return self._reject(sig, "in-exit refused: %s" % e, meta)
        pstop, ptarget = plan["premium_stop"], plan["target_premium"]
        meta["plan"] = {k: plan.get(k) for k in ("premium_stop", "target_premium", "underlying_stop",
                                                 "target_underlying", "flat_by", "no_progress_bars", "r_value_inr",
                                                 "in_exit_underlying_stop", "in_exit_check")}

        # in-budget: lots from the max loss, charges included
        ch = smart.read_json(self.p["charges"], {}) or {}
        z = paper.sizing(entry, pstop, inst, cfg, ch)
        meta.update(lots=z["lots"], sized_by=z["decided"], max_loss_inr=float(z["max_loss"]),
                    risk_inr=float(z["premium_risk"] + z["charges"]))
        if z["lots"] < 1:
            return self._reject(sig, "one lot of %s risks more than the max loss Rs %s (%s)" % (
                inst.get("tradingsymbol"), z["max_loss"], z["decided"]), meta)

        # in-timemachine again, now with one lot's charges in R taken off every replayed trade
        if timemachine.settings(cfg)["enabled"]:
            gross = self.timemachine_verdict(sym, sig["strategy"], now, cfg)
            try:
                charges_r = tm.plan_charges(plan, ch)["in_r"]
            except smart.SmartError:
                charges_r = None
            if charges_r is not None and gross.get("data"):
                net = timemachine.with_charges(gross, charges_r, cfg)
                meta["timemachine"] = timemachine.summary(net)
                meta["timemachine"]["charges_r"] = charges_r
                log.info("TIME MACHINE %s %s after charges: %s", "PASS" if net["ok"] else "BLOCK", sig["id"],
                         net["reason"])
                if not net["ok"]:
                    return self._reject(sig, "time machine: %s" % net["reason"], meta)

        # in-risk: every check on the ticket in-budget would draft; a BLOCK is final
        tid = self.store.next_trade_id(self.day)
        ticket = size.draft(plan, z, tid, now)
        fresh = self.await_fresh_ticks(inst, sym, cfg)
        if fresh:
            meta["feed_wait"] = fresh
        result = self.risk_check(ticket, plan, cfg, smart.now_ist())
        meta["risk"] = {"verdict": result["verdict"], "reasons": result["reasons"],
                        "advisory": result.get("advisory", []),
                        "checks": [{"name": c["name"], "ok": c["ok"], "detail": c["detail"]} for c in result["checks"]]}
        log.info("RISK %s %s%s", result["verdict"], sig["id"], ": " + "; ".join(result["reasons"]) if result["reasons"] else "")
        if result["verdict"] == "BLOCK":
            return self._reject(sig, "in-risk BLOCK: %s" % "; ".join(result["reasons"]), meta)

        flat = plan["flat_by"]
        t = {"id": tid, "signal_id": sig["id"], "date": self.day,
             "strategy": sig["strategy"], "symbol": sym, "direction": side, "tradingsymbol": inst["tradingsymbol"],
             "token": str(inst["token"]), "segment": inst["segment"], "instrument": inst, "lots": z["lots"],
             "quantity": sum(z["qtys"]), "order_qtys": z["qtys"], "entry_price": entry, "entry_time": now.isoformat(),
             "entry_underlying": sig["entry_underlying"], "stop_premium": pstop, "target_premium": ptarget,
             "stop_underlying": sig["stop_underlying"], "target_underlying": sig["target_underlying"],
             "flat_by": flat, "status": "OPEN", "no_progress_bars": plan.get("no_progress_bars")}
        self.store.open_trade(t, sig["id"])
        self.store.set_signal(sig["id"], "EXECUTED", None, t["id"], meta)
        log.info("PAPER ENTRY %s %s %s %s x%d @ %.2f SL %.2f TGT %.2f (underlying SL %.2f TGT %.2f)", t["id"],
                 t["strategy"], t["tradingsymbol"], side, t["quantity"], entry, pstop, ptarget,
                 t["stop_underlying"], t["target_underlying"])
        self.emit("trade_opened", self.store.trade(t["id"]))
        self.update_subscriptions(cfg)

    # ------------------------------------------------------------ in-exit and in-risk
    def engine_paths(self):
        """smart.trader_paths with entry.json and plan.json moved to state/engine/: the engine's pipeline never
        overwrites the files of a trade the user plans by hand with the skills."""
        d = os.path.join(self.p["state"], "engine")
        os.makedirs(d, exist_ok=True)
        return dict(self.p, entry=os.path.join(d, "entry.json"), plan=os.path.join(d, "plan.json"))

    def exit_plan(self, sig, s, premium, cfg, now):
        """in-exit's make_plan for this signal: writes the in-entry shaped entry.json it reads, returns plan.json.
        The underlying stop and target stay the signal's (ORB is cross-checked against in-exit's or-mid/or-opposite;
        EMA_PULLBACK's swing stop is not an in-exit rule, so it is given to in-exit as the equal percentage)."""
        p = self.engine_paths()
        rng = sig.get("opening_range") or {"high": sig["entry_underlying"], "low": sig["entry_underlying"]}
        smart.write_json(p["entry"], {
            "created_by": "in-entry", "symbol": sig["symbol"], "date": sig["date"], "status": "triggered",
            "direction": sig["direction"], "trigger_level": rng["high"] if sig["direction"] == "bullish" else rng["low"],
            "bar_start_ist": sig["bar_start_ist"], "bar_close_ist": sig["bar_close_ist"],
            "bar_close": sig["entry_underlying"], "opening_range": dict(rng, start="09:15", end=(
                cfg.get("session") or {}).get("range_end", "09:30")), "filters_applied": [],
            "notes": ["paper engine %s signal %s" % (sig["strategy"], sig["id"])], "source": "engine",
            "as_of_ist": now.isoformat()})
        orb = sig["strategy"] == "ORB" and bool(sig.get("opening_range"))
        if orb:
            stop = s.get("stop_rule") or "or-mid"
        else:
            stop = "pct:%.6f" % (abs(sig["entry_underlying"] - sig["stop_underlying"]) / sig["entry_underlying"] * 100)
        a = argparse.Namespace(trader=self.trader, stop=stop, entry_premium=premium, offline=None, now=None)
        pcfg = dict(cfg, exit=dict(cfg.get("exit") or {}, target_r=float(s.get("target_r") or 2.0)))
        plan, _, _ = exitplan.make_plan(a, p, pcfg, now)
        plan["in_exit_underlying_stop"] = plan["underlying_stop"]
        if orb:
            plan["in_exit_check"] = "match" if abs(plan["underlying_stop"] - sig["stop_underlying"]) < 0.001 else \
                "in-exit stop %.2f, engine %.2f" % (plan["underlying_stop"], sig["stop_underlying"])
            if plan["in_exit_check"] != "match":
                log.warning("in-exit cross-check %s: %s", sig["id"], plan["in_exit_check"])
        plan.update(underlying_stop=sig["stop_underlying"], target_underlying=sig["target_underlying"])
        smart.write_json(p["plan"], plan)
        return plan

    def await_fresh_ticks(self, inst, sym, cfg):
        """in-risk's stale_data check reads the feed's last tick of the option and its index. A just-picked
        option may not have ticked yet: subscribe it and wait up to FRESH_WAIT_SEC for both ticks to be younger
        than freshness.stale_tick_sec. None without a live feed (in-risk then judges the pick's own age)."""
        if not self.feed.status()["alive"]:
            return None
        tokens = [str(inst["token"])] + ([self.facts[sym]["token"]] if sym in self.facts else [])
        smart.subscribe(self.trader, "engine:pending", [{"segment": inst["segment"], "token": str(inst["token"]),
                                                         "mode": "quote"}])
        stale_ms = float((cfg.get("freshness") or {}).get("stale_tick_sec", 3)) * 1000
        t0 = time.time()
        while True:
            latest = smart.read_latest(self.p["db"], tokens, int(time.time() * 1000), stale_ms)
            if all(t in latest and not latest[t]["stale"] for t in tokens):
                return "fresh ticks after %.1f s" % (time.time() - t0)
            if time.time() - t0 >= FRESH_WAIT_SEC or self.stop_event.is_set():
                late = [t for t in tokens if t not in latest or latest[t]["stale"]]
                log.warning("no fresh tick for %s within %d s: in-risk decides on the feed as it is", late,
                            FRESH_WAIT_SEC)
                return "still stale after %d s: %s" % (FRESH_WAIT_SEC, ", ".join(late))
            self.stop_event.wait(0.2)

    def risk_check(self, ticket, plan, cfg, now):
        """in-risk's verdict() on the ticket, with the paper journal as today's trades. Checks named in
        risk.engine_advisory_checks are recorded but do not block (default: none, every check blocks)."""
        p = self.p
        today = now.date().isoformat()
        journal = [{"date": t["date"], "fill_price": t["entry_price"], "net_pnl_inr": t.get("net_pnl"),
                    "exit_time_ist": (t.get("exit_time") or "")[11:16] or None}
                   for t in self.store.trades(today) if t.get("entry_price") is not None]
        events = smart.read_json(p["events"])
        rows = self.master_rows or []
        x = {"cfg": cfg, "capital": smart.require_account(cfg), "now": now,
             "calendar": smart.read_json(p["calendar"], {}) or {}, "trades": journal, "ban": risk.ban_list(p["ban"]),
             "events": None if events is None else events.get("events", []), "pick": smart.read_json(p["pick"]),
             "entry": smart.read_json(self.engine_paths()["entry"]), "db": p["db"], "rows": rows,
             "master": {(smart.segment(r.get("exch_seg")), str(r.get("token"))): r for r in rows}}
        try:
            result = risk.verdict(ticket, x)
        except smart.SmartError as e:                       # a limit missing from config.json: never a silent PASS
            return {"verdict": "BLOCK", "reasons": ["config: %s" % e], "checks": [], "checked_at_ist": now.isoformat()}
        soft = set((cfg.get("risk") or {}).get("engine_advisory_checks") or [])
        hard = [c for c in result["checks"] if not c["ok"] and c["name"] not in soft]
        result["advisory"] = ["%s: %s" % (c["name"], c["detail"]) for c in result["checks"]
                              if not c["ok"] and c["name"] in soft]
        result["verdict"] = "BLOCK" if hard else "PASS"
        result["reasons"] = ["%s: %s" % (c["name"], c["detail"]) for c in hard]
        return result

    # ------------------------------------------------------------ in-timemachine
    def timemachine_window(self, sym, now, cfg, cal=None):
        """The rolling window of the last N closed trading days for sym: drops the oldest, fetches only new or
        missing days. Never raises: a failure comes back as an empty window that names it."""
        cal = cal if cal is not None else (smart.read_json(self.p["calendar"], {}) or {})
        f = self.facts.get(sym)
        if not f:
            return {"dates": [], "bars": {}, "missing": [], "partial": "%s is not in today's instrument master" % sym}
        try:
            if self.tm_window is None:
                self.tm_window = timemachine.Window(self.trader)
            end = smart.previous_trading_day(now.date(), cal).isoformat()
            return timemachine.ensure_timemachine_window(
                self.trader, end, sym, f["segment"], f["token"], self.client, now,
                int(timemachine.settings(cfg)["days"]), cal, store=self.tm_window)
        except Exception as e:                               # a broker or disk failure never ends the cycle
            log.warning("time machine window for %s failed: %s", sym, e)
            return {"dates": [], "bars": {}, "missing": [], "partial": "%s: %s" % (type(e).__name__, str(e)[:200])}

    def timemachine_verdict(self, sym, strategy, now, cfg):
        """in-timemachine's replay of `strategy` on sym's window: {"ok", "reason", "data"}, once per day and rule."""
        key = json.dumps([self.day, sym, strategy, strat.strategy_cfg(cfg, strategy), cfg.get("session"),
                          timemachine.settings(cfg)], sort_keys=True, default=str)
        if key in self.tm_cache:
            return self.tm_cache[key]
        window = self.timemachine_window(sym, now, cfg)
        doc = timemachine.check(self.trader, sym, strategy, cfg, window, (self.facts.get(sym) or {}).get("expiries", ()))
        if doc["data"] is not None:                          # an unavailable replay is retried at the next signal
            self.tm_cache[key] = doc
        return doc

    # ------------------------------------------------------------ the backtest
    def backtest(self, symbol, strategy, days, overrides=None):
        """Replay `strategy` on `symbol`'s last `days` closed trading days with these strategy settings
        (the saved ones, with `overrides` on top). History needs a broker session: one is opened if the
        engine has none (outside market hours too: history is exempt from the market-hours rule)."""
        now = smart.now_ist()
        cfg = smart.load_config(self.trader)
        cal = smart.read_json(self.p["calendar"], {}) or {}
        if symbol not in DEFAULT_SYMBOLS:
            raise EngineError("symbol must be one of %s" % ", ".join(DEFAULT_SYMBOLS))
        if overrides:
            cfg = dict(cfg, strategies=dict(cfg.get("strategies") or {}, **{
                strategy: {**((cfg.get("strategies") or {}).get(strategy) or {}), **overrides}}))
        with self.op:
            if not self.ensure_client(now):
                raise EngineError("no broker session for history: %s" % (self.angel.get("last_error") or "login failed"))
            if symbol not in self.facts or self.day != now.date().isoformat():
                self.ensure_day(now, dict(cfg, paper=dict(cfg.get("paper") or {}, symbols=sorted(
                    set(self.symbols(cfg)) | {symbol}))), cal)
            f = self.facts.get(symbol)
            client = self.client
        if not f:
            raise EngineError("%s is not in today's instrument master" % symbol)
        try:
            return timemachine.backtest(self.trader, symbol, f["segment"], f["token"], client, cfg, strategy, days,
                                        now, cal, f.get("expiries", ()))
        except smart.SmartError as e:
            raise EngineError(str(e))

    def research_inputs(self, days, symbols, premiums=None, option_slippage_pct=None):
        """History windows (one seed day + `days` days with bars), expiries, costs and 1R in rupees for the
        comparison and the sweep. One broker session, history from the candle cache when it is there."""
        now = smart.now_ist()
        cfg = smart.load_config(self.trader)
        cal = smart.read_json(self.p["calendar"], {}) or {}
        bad = [s for s in symbols if s not in DEFAULT_SYMBOLS]
        if bad or not symbols:
            raise EngineError("symbols must be a non-empty subset of %s" % ", ".join(DEFAULT_SYMBOLS))
        with self.op:
            if not self.ensure_client(now):
                raise EngineError("no broker session for history: %s" % (self.angel.get("last_error") or "login failed"))
            if any(s not in self.facts for s in symbols) or self.day != now.date().isoformat() or not self.master_rows:
                self.ensure_day(now, dict(cfg, paper=dict(cfg.get("paper") or {}, symbols=sorted(
                    set(self.symbols(cfg)) | set(symbols)))), cal)
            facts, client, rows = dict(self.facts), self.client, list(self.master_rows or [])
        windows = {}
        try:
            for s in symbols:
                f = facts.get(s)
                if not f:
                    raise EngineError("%s is not in today's instrument master" % s)
                windows[s] = timemachine.history_window(self.trader, f["segment"], f["token"], client, days, now, cal)
            lots = research.lots_from_master(rows, symbols, now.date())
            missing = [s for s in symbols if not lots.get(s)]
            if missing:
                raise EngineError("no option lot size in today's master for %s" % ", ".join(missing))
            ch = smart.read_json(self.p["charges"], {}) or {}
            costs = research.cost_model(cfg, ch, lots, premiums, option_slippage_pct)
            acct = cfg.get("account") or {}
            risk_inr = float(smart.require_account(cfg)) * float(acct.get("risk_per_trade_pct", 1.0)) / 100
        except smart.SmartError as e:
            raise EngineError(str(e))
        return cfg, windows, {s: facts[s].get("expiries", ()) for s in symbols}, costs, risk_inr, now

    def backtest_compare(self, days, symbols, premiums=None, option_slippage_pct=None, stop_slippage_r=None):
        cfg, windows, expiries, costs, risk_inr, now = self.research_inputs(days, symbols, premiums,
                                                                            option_slippage_pct)
        out = research.compare(windows, cfg, expiries, costs, risk_inr, stop_slippage_r)
        out.update(as_of_ist=now.isoformat(), days=days, symbols=symbols, options=OPTIONS_NOTE)
        return research.finite(out)

    def backtest_sweep(self, days, symbols, version="EMA_ORB_C", premiums=None, option_slippage_pct=None,
                       stop_slippage_r=None):
        cfg, windows, expiries, costs, risk_inr, now = self.research_inputs(days, symbols, premiums,
                                                                            option_slippage_pct)
        out = research.sweep(windows, cfg, expiries, costs, risk_inr, version, stop_slippage_r)
        out.update(as_of_ist=now.isoformat(), days=days, symbols=symbols, options=OPTIONS_NOTE)
        return research.finite(out)

    # ------------------------------------------------------------ the UI's view
    def snapshot(self):
        cfg = smart.read_json(self.p["config"], {}) or {}
        ch = smart.read_json(self.p["charges"], {}) or {}
        now = smart.now_ist()
        today = now.date().isoformat()
        opens = []
        for t in self.store.open_trades():
            ltp = paper.valid_price(t.get("ltp"))
            if ltp is not None:
                g, c, n = paper.pnl(t["entry_price"], ltp, t["order_qtys"], t["segment"], ch)
                t.update(unrealized_gross=round(g, 2), unrealized_net=round(n, 2), est_charges=round(c, 2))
            opens.append(t)
        keys = list(strat.STRATEGIES)
        sess, pap = cfg.get("session") or {}, cfg.get("paper") or {}
        fs = self.feed.status()
        lag = fs.get("lag_ms_last_minute") or {}
        return {
            "server_time_ist": now.isoformat(), "today": today,
            "engine": dict(self.status, running=self.running,
                           desired_running=self.store.get("desired_running", "1") == "1"),
            "angel": dict(self.angel, feed={
                "alive": fs["alive"], "managed": fs["managed"], "connected": bool(fs.get("connected")) and fs["alive"],
                "reconnects": fs.get("reconnects", 0), "ticks": fs.get("ticks", 0), "subscribed": fs.get("subscribed"),
                "lag_median_ms": lag.get("median"), "lag_worst_ms": lag.get("worst"), "stale": fs.get("stale", 0),
                "last_error": fs.get("last_error"), "updated_ist": fs.get("updated_ist"), "restarts": fs["restarts"]}),
            "market": [dict(self.market[s], stale=bool(self.market[s].get("stale")) or not self.running)
                       for s in self.symbols(cfg) if s in self.market],
            "config": {"entry_start": sess.get("entry_start", "09:30"), "entry_cutoff": sess.get("entry_end", "14:00"),
                       "flat_by": sess.get("flat_by", "15:10"), "auto_execute": pap.get("auto_execute", True),
                       "symbols": self.symbols(cfg), "capital_inr": (cfg.get("account") or {}).get("capital_inr")},
            "strategies": [{"key": k, "name": strat.STRATEGIES[k]["name"], **strat.strategy_cfg(cfg, k)} for k in keys],
            "signals": self.store.signals(today, 200),
            "open_trades": opens,
            "closed_trades": self.store.trades(today, "CLOSED", 200),
            "stats_today": self.store.stats(today, keys),
            "stats_all_time": self.store.stats(None, keys),
            "unrealized_net": round(sum(t.get("unrealized_net") or 0 for t in opens), 2),
        }


engine = Engine()

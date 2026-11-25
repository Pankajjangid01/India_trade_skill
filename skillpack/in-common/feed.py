#!/usr/bin/env python3
"""The one live feed: owns the SmartStream WebSocket and writes every tick to
trader/state/ticks.sqlite. Other skills read that file (smart.read_latest,
smart.read_bars); none opens its own connection. Standard library only.
Read-only market data: it never places an order.

    python in-common/feed.py run                                   # until 15:45 IST, from state/subscriptions.json
    python in-common/feed.py run --underlyings NIFTY,SENSEX --strikes 2 --minutes 30
    python in-common/feed.py run --minutes 30 --drop-every 600     # the Phase 1 gate: forced reconnect every 10 min
    python in-common/feed.py status                                # lag, staleness, reconnects
    python in-common/feed.py run --offline FRAMES.jsonl --now "2026-11-24 10:20"

Subscriptions: a skill that wants live data writes its tokens under its own
name in trader/state/subscriptions.json (smart.subscribe). The feed reads the
union every 5 s and subscribes or unsubscribes the difference, at most
limits.ws_max_tokens (900) tokens on one connection. --underlyings adds the
indices (Quote) and ATM +/- --strikes nearest-expiry options (SnapQuote).

It reconnects with backoff (1, 2, 4 ... 60 s plus jitter) and re-subscribes
everything. Two refusals in a row (usually the token) stop it with the next
step. It writes state/feed_status.json every 2 s: connected, reconnects,
ticks, feed lag (receipt minus exchange time, median and worst over the last
minute) and stale tokens (no tick for freshness.stale_tick_sec). Live, it runs
only on trading days, 09:00 to 15:45 IST, and only after in-setup's probe has
confirmed the stream. One feed per trader/ folder.

Offline, FRAMES is a JSON-lines replay; a {"drop": true} line ends one
connection and the next lines are the reconnect.

Exit codes: 0 = ran and stopped on time, 1 = could not start or was refused (one line why).
"""

import argparse
import collections
import datetime
import json
import os
import random
import socket
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import smart  # noqa: E402

STATUS_EVERY_MS = 2000
FLUSH_EVERY_MS = 1000
SYNC_EVERY_MS = 5000
LAG_WINDOW_MS = 60000
ALIVE_MS = 15000          # a status younger than this means another feed is running
BACKOFF_MAX = 60.0


class Dropped(Exception):
    """This connection is over; reconnect."""


def paths(trader):
    state = os.path.join(trader, "state")
    return {"state": state, "db": os.path.join(state, "ticks.sqlite"), "status": os.path.join(state, "feed_status.json"),
            "session": os.path.join(state, "session.json"), "config": os.path.join(trader, "config.json"),
            "calendar": os.path.join(trader, "calendar.json"), "cache": os.path.join(trader, "cache")}


def ist(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, smart.IST).isoformat(timespec="seconds")


class Feed:
    """The connection loop. `connect()` returns a stream (WebSocket or ReplayStream),
    optionally with the auth form that worked, or raises StopIteration when an
    offline replay has no more connections. With replay_from_ms the clock is the
    replay's own receipt times, so a test runs in no time and gives the same answer."""

    def __init__(self, store, status_path, connect, wanted, stale_ms, ping_ms=10000, drop_every_ms=None,
                 replay_from_ms=None, sleep=time.sleep, rand=random.random):
        self.store, self.status_path, self.connect, self.wanted = store, status_path, connect, wanted
        self.stale_ms, self.ping_ms, self.drop_every_ms = stale_ms, ping_ms, drop_every_ms
        self.replay = replay_from_ms is not None
        self.last_ms = replay_from_ms or 0
        self.sleep, self.rand = sleep, rand
        self.lags = collections.deque()
        self.seen = {}           # token -> last receipt ms, for staleness
        self.have = {}           # (exchange code, token) -> mode, on the current connection
        self.connected_at = None
        self.st = {"running": True, "pid": os.getpid(), "connected": False, "connects": 0, "reconnects": 0,
                   "subscribed": 0, "dropped_over_cap": 0, "ticks": 0, "server_text": [], "last_error": None,
                   "auth_header": None}

    def clock_ms(self):
        return self.last_ms if self.replay else int(time.time() * 1000)

    # ---- status

    def lag_stats(self, now):
        while self.lags and now - self.lags[0][0] > LAG_WINDOW_MS:
            self.lags.popleft()
        v = [lag for _, lag in self.lags]
        return {"median": int(statistics.median(v)), "worst": max(v), "ticks": len(v)} if v else \
            {"median": None, "worst": None, "ticks": 0}

    def stale_tokens(self, now):
        """Subscribed tokens with no tick for the stale limit (a token that never ticked counts
        once the connection is older than the limit)."""
        out = []
        for _, token in self.have:
            at = self.seen.get(token, self.connected_at)
            if at is not None and now - at > self.stale_ms:
                out.append(token)
        return sorted(out)

    def write_status(self, now, final=False):
        stale = self.stale_tokens(now) if self.st["connected"] else []
        self.st.update(updated_ist=ist(now), updated_ms=now, lag_ms_last_minute=self.lag_stats(now),
                       stale=len(stale), stale_tokens=stale[:20])
        if final:
            self.st.update(running=False, connected=False)
        try:
            smart.write_json(self.status_path, self.st)
        except OSError:   # a reader holding the file on Windows: the next round writes it
            pass

    # ---- subscriptions

    def sync(self, conn, have):
        """Subscribe what is wanted and not yet subscribed; unsubscribe what is no longer wanted."""
        want, dropped = self.wanted()
        self.st["dropped_over_cap"] = dropped
        add = collections.defaultdict(lambda: collections.defaultdict(list))
        gone = collections.defaultdict(lambda: collections.defaultdict(list))
        for (code, token), mode in want.items():
            if have.get((code, token)) != mode:
                if (code, token) in have:
                    gone[have[(code, token)]][code].append(token)
                add[mode][code].append(token)
        for (code, token), mode in have.items():
            if (code, token) not in want:
                gone[mode][code].append(token)
        for action, groups in ((0, gone), (1, add)):
            for mode, by_code in sorted(groups.items()):
                conn.send(json.dumps({"correlationID": "feed%d%d" % (action, mode), "action": action,
                                      "params": {"mode": mode, "tokenList": [{"exchangeType": c, "tokens": t}
                                                                             for c, t in sorted(by_code.items())]}}))
        have.clear()
        have.update(want)
        self.st["subscribed"] = len(have)

    # ---- the loop

    def run(self, until_ms):
        """Connect, stream, reconnect, until until_ms. A connection that lasted under 30 s
        doubles the wait before the next (1, 2, 4 ... 60 s plus jitter)."""
        backoff, refused = 1.0, 0
        while self.clock_ms() < until_ms:
            try:
                conn = self.connect()
            except StopIteration:
                break
            except smart.StreamRefused as e:
                refused += 1
                self.st["last_error"] = str(e)
                if refused >= 2:
                    self.write_status(self.clock_ms(), final=True)
                    raise smart.SmartError("%s, twice in a row: run in-setup login, then start the feed again" % e)
                self._wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            except (smart.SmartError, OSError) as e:
                self.st["last_error"] = str(e)
                self._wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            refused = 0
            if isinstance(conn, tuple):
                conn, self.st["auth_header"] = conn
            self.st["connects"] += 1
            self.st["reconnects"] = self.st["connects"] - 1
            self.st["connected"] = True
            self.connected_at = started = self.clock_ms()
            try:
                self.session(conn, until_ms)
                break                       # stopped on time
            except (Dropped, OSError, ValueError) as e:
                self.st["last_error"] = str(e) or e.__class__.__name__
            finally:
                self.st["connected"] = False
                self.store.flush()
                try:
                    conn.close()
                except OSError:
                    pass
            self.write_status(self.clock_ms())
            backoff = 1.0 if self.clock_ms() - started > 30000 else min(backoff * 2, BACKOFF_MAX)
            self._wait(backoff)
        self.store.flush()
        self.write_status(self.clock_ms(), final=True)
        return self.st

    def _wait(self, seconds):
        self.sleep(seconds + self.rand())

    def session(self, conn, until_ms):
        have = self.have
        have.clear()
        self.sync(conn, have)
        start = last_ping = last_flush = last_sync = last_status = self.clock_ms()
        conn.send("ping")
        while True:
            now = self.clock_ms()
            if now >= until_ms:
                return
            if self.drop_every_ms and now - start >= self.drop_every_ms:
                raise Dropped("forced disconnect (--drop-every)")
            if now - last_ping >= self.ping_ms:
                conn.send("ping")
                last_ping = now
            if now - last_sync >= SYNC_EVERY_MS:
                self.sync(conn, have)
                last_sync = now
            if now - last_flush >= FLUSH_EVERY_MS:
                self.store.flush()
                last_flush = now
            if now - last_status >= STATUS_EVERY_MS:
                self.write_status(now)
                last_status = now
            try:
                op, data, ms = conn.recv(timeout=1.0)
            except socket.timeout:
                continue
            if self.replay:
                self.last_ms = max(self.last_ms, ms)
            if op == 8:
                raise Dropped("closed by the server")
            if op == 1:
                text = data.decode("utf-8", "replace").strip()
                if text != "pong":
                    self.st["server_text"] = (self.st["server_text"] + [text[:200]])[-5:]
                continue
            try:
                tick = smart.parse_tick(data)
            except ValueError:
                continue
            self.store.add(tick, ms)
            self.seen[tick["token"]] = ms
            self.lags.append((ms, ms - tick["exch_ms"]))
            self.st["ticks"] += 1


# ---------------------------------------------------------------- commands

def replay_connections(path):
    """Offline: one ReplayStream per connection; a {"drop": true} line splits them."""
    sessions, cur = [], []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            f = json.loads(line)
            if f.get("drop"):
                sessions.append(cur)
                cur = []
            else:
                cur.append(f)
    sessions.append(cur)
    streams = iter([smart.ReplayStream(s, end_with_close=True) for s in sessions])
    return lambda: next(streams)


def chain_items(rows, name, spot, scale, today, n):
    """The index (Quote) and ATM +/- n nearest-expiry CE and PE (SnapQuote) of one underlying."""
    facts = smart.underlying_facts(rows, name, today, scale)
    items = [{"segment": facts["index_segment"], "token": facts["index_token"], "mode": "quote"}] \
        if facts.get("index_token") else []
    by_exp = smart.index_options(rows, name, today)
    if not by_exp or not scale:
        return items
    near = by_exp[min(by_exp)]
    strikes = sorted({float(r["strike"]) / scale for r in near})
    atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot)) if spot else len(strikes) // 2
    pick = set(strikes[max(0, atm - n):atm + n + 1])
    for r in near:
        if float(r["strike"]) / scale in pick:
            items.append({"segment": smart.segment(r["exch_seg"]), "token": r["token"], "mode": "snapquote"})
    return items


def live_setup(a, p, cfg, cal, now):
    """Checks, login and subscriptions for a live run. Returns the connect function."""
    allowed, why = smart.market_data_allowed(now, cal)
    if not allowed:
        raise smart.SmartError("market data is %s: the feed runs on trading days, 09:00 to 15:45 IST" % why)
    smart.require(a.trader, "stream")
    creds = smart.credentials()
    api = cfg.get("api") or {}
    ips = {"local": smart.local_ip(), "mac": smart.mac_address(),
           "public": smart.public_ip(api.get("client_public_ip"), p["state"], now.date().isoformat())}
    client = smart.Client(creds["api_key"], limiter=smart.Limiter(p["state"], smart.caps_from_config(cfg.get("limits"))),
                          ips=ips)
    sess = smart.Session(client, creds, p["session"], now.date().isoformat())
    sess.start()
    if a.underlyings:
        rows = smart.load_master(p["cache"], now.date().isoformat())
        scale = smart.strike_scale(rows)[0]
        names = [u.strip().upper() for u in a.underlyings.split(",") if u.strip()]
        facts = {u: smart.underlying_facts(rows, u, now.date(), scale) for u in names}
        tokens = collections.defaultdict(list)
        for f in facts.values():
            if f.get("index_token"):
                tokens[f["index_segment"]].append(f["index_token"])
        r = client.call("quote", {"mode": "LTP", "exchangeTokens": dict(tokens)})
        spot = {str(x.get("symbolToken")): x.get("ltp") for x in ((r.data or {}).get("fetched") or [])} if r.ok else {}
        items = []
        for u in names:
            items += chain_items(rows, u, spot.get(facts[u].get("index_token")), scale, now.date(), a.strikes)
        smart.subscribe(a.trader, "feed-cli", items)

    def connect():
        return smart.open_stream(smart.read_json(p["session"], {}), creds)
    return connect


def cmd_run(a):
    p = paths(a.trader)
    cfg = smart.read_json(p["config"]) or {}
    cal = smart.read_json(p["calendar"]) or {}
    now = smart.now_ist(a.now)
    status = smart.read_json(p["status"]) or {}
    wall = int(time.time() * 1000)
    if not a.offline and status.get("running") and wall - (status.get("updated_ms") or 0) < ALIVE_MS:
        raise smart.SmartError("a feed is already running for %s (pid %s): one feed per folder"
                               % (a.trader, status.get("pid")))
    limits, fresh = cfg.get("limits") or {}, cfg.get("freshness") or {}
    cap = int(limits.get("ws_max_tokens", 900))
    stale_ms = int(float(fresh.get("stale_tick_sec", 3)) * 1000)
    if a.offline:
        connect, until = replay_connections(a.offline), float("inf")
    else:
        connect = live_setup(a, p, cfg, cal, now)
        stop = (cfg.get("session") or {}).get("data_calls_stop", "15:45")
        hh, mm = (int(x) for x in stop.split(":"))
        until = int(now.replace(hour=hh, minute=mm, second=0).timestamp() * 1000)
        if a.minutes:
            until = min(until, int(time.time() * 1000) + a.minutes * 60000)
    store = smart.TickStore(p["db"])
    store.prune(int(now.timestamp() * 1000) - 10 * 86400000)
    feed = Feed(store, p["status"], connect, lambda: smart.wanted(a.trader, cap), stale_ms,
                drop_every_ms=a.drop_every * 1000 if a.drop_every else None,
                replay_from_ms=int(now.timestamp() * 1000) if a.offline else None,
                sleep=(lambda s: None) if a.offline else time.sleep)
    try:
        st = feed.run(until)
    finally:
        store.close()
    print("\n".join(summary_lines(st, a.trader)))
    return 0


def summary_lines(st, trader):
    lag = st.get("lag_ms_last_minute") or {}
    out = ["Feed for %s/ (%s)" % (trader.replace(os.sep, "/"), "running" if st.get("running") else "stopped"),
           "  Not financial advice: live market data only; it never places an order.",
           "  %-14s %s" % ("Updated", st.get("updated_ist")),
           "  %-14s %d connect(s), %d reconnect(s); last error: %s" % (
               "Connects", st.get("connects", 0), st.get("reconnects", 0), st.get("last_error") or "none"),
           "  %-14s %d tokens%s" % ("Subscribed", st.get("subscribed", 0),
                                    ", %d over the cap left out" % st["dropped_over_cap"] if st.get("dropped_over_cap") else ""),
           "  %-14s %d" % ("Ticks", st.get("ticks", 0)),
           "  %-14s %s" % ("Feed lag", "median %s ms, worst %s ms over the last minute (%d ticks)" % (
               lag.get("median"), lag.get("worst"), lag.get("ticks", 0)) if lag.get("ticks") else "no ticks in the last minute"),
           "  %-14s %d%s" % ("Stale", st.get("stale", 0), " (%s)" % ", ".join(st["stale_tokens"]) if st.get("stale") else "")]
    if st.get("server_text"):
        out.append("  %-14s %s" % ("Server said", " | ".join(st["server_text"])))
    return out


def cmd_status(a):
    p = paths(a.trader)
    st = smart.read_json(p["status"])
    if not st:
        print("feed: no feed has run in %s/ yet" % a.trader.replace(os.sep, "/"), file=sys.stderr)
        return 1
    if st.get("running") and int(time.time() * 1000) - (st.get("updated_ms") or 0) > ALIVE_MS:
        st = dict(st, running=False, last_error=(st.get("last_error") or "") + " (status is old: the feed died?)")
    print("\n".join(summary_lines(st, a.trader)))
    return 0


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--trader", default="trader", help="the working folder (default ./trader)")
    ap = argparse.ArgumentParser(description="The one live feed (read-only, no orders).")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("run", parents=[common], help="stream until 15:45 IST or --minutes")
    s.add_argument("--underlyings", help="e.g. NIFTY,SENSEX: the index and ATM +/- --strikes options")
    s.add_argument("--strikes", type=int, default=2)
    s.add_argument("--minutes", type=int, help="stop after this many minutes")
    s.add_argument("--drop-every", type=int, metavar="SEC", help="close the connection every SEC seconds (gate test)")
    s.add_argument("--offline", metavar="FRAMES", help="replay a JSON-lines frame file")
    s.add_argument("--now", help='pretend it is this IST time, e.g. "2026-11-24 10:20"')
    sub.add_parser("status", parents=[common], help="the last status the feed wrote")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "status": cmd_status}[a.cmd](a)
    except smart.SmartError as e:
        print("feed: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())

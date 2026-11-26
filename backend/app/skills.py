"""The skill runner: every skill pack command a person can run from the UI, one shot at a time.

Each command runs the skill's own script in a subprocess (python <skill>/<script> <command> ... --trader DIR):
its prints, sys.argv and any network wait stay out of the engine's process, and a timeout ends a stuck call.
Only the commands and arguments listed in COMMANDS are accepted, each value checked, so no request can pass a
path or a flag of its own. Long-running watch commands are not offered (the engine itself watches).

Every run is written to trader/state/skills/<skill>_<command>.json as
    {"ok": exit code 0, "reason": the report's last line, "data": {"exit_code", "stdout", "stderr", "argv",
     "outputs": {file: the JSON the skill wrote}, "started_ist", "seconds"}}
"""

import logging
import os
import re
import subprocess
import sys
import threading
import time

from app import config
from app.sp import SCRIPTS, smart

log = logging.getLogger("skills")
TIMEOUT_SEC = 180
SYMBOL = re.compile(r"^[A-Z0-9&-]{1,20}$")
TICKET = re.compile(r"^[A-Z]\d{8}-\d{1,4}$")
QUARTER = re.compile(r"^Q[1-4]FY\d{2}$")
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
INDICES = ("NIFTY", "SENSEX", "BANKNIFTY")


class SkillError(ValueError):
    pass


# ---------------------------------------------------------------- argument checks

def _sym(v, indices_only=False):
    v = str(v or "").strip().upper()
    if not SYMBOL.match(v) or (indices_only and v not in INDICES):
        raise SkillError("symbol must be %s" % ("one of " + ", ".join(INDICES) if indices_only else "an NSE name"))
    return v


def _num(v, name, lo=None, hi=None, integer=False):
    try:
        x = int(v) if integer else float(v)
    except (TypeError, ValueError):
        raise SkillError("%s must be a number" % name)
    if (lo is not None and x < lo) or (hi is not None and x > hi) or x != x:
        raise SkillError("%s must be %s to %s" % (name, lo, hi))
    return str(x)


def _match(v, rx, name):
    v = str(v or "").strip()
    if not rx.match(v):
        raise SkillError("%s is not valid" % name)
    return v


def _stop(v):
    v = str(v or "").strip().lower()
    if not re.match(r"^(or-mid|or-opposite|(pct|atr):\d{1,2}(\.\d{1,3})?)$", v):
        raise SkillError("stop must be or-mid, or-opposite, pct:X or atr:K")
    return v


def _text(v, name, limit=120):
    v = str(v or "").strip()
    if not v or len(v) > limit or v.startswith("-") or any(ord(c) < 32 for c in v):
        raise SkillError("%s must be 1 to %d plain characters" % (name, limit))
    return v


def _opt(a, key, fn, flag, out):
    if a.get(key) not in (None, ""):
        out += [flag, fn(a[key])]
    return out


# ---------------------------------------------------------------- the commands

def _entry_check(a):
    out = ["check", _sym(a.get("symbol"), True)]
    for f in a.get("filters") or []:
        if f not in ("volume", "vwap", "trend"):
            raise SkillError("filter must be volume, vwap or trend")
        out += ["--filter", f]
    return out


def _news(a):
    out = []
    if a.get("symbols"):
        out += ["--symbols", ",".join(_sym(s) for s in str(a["symbols"]).split(",") if s.strip())]
    return _opt(a, "hours", lambda v: _num(v, "hours", 1, 72), "--hours", out)


def _ticket_levels(cmd):
    def build(a):
        out = [cmd, _match(a.get("id"), TICKET, "ticket id"), "--price", _num(a.get("price"), "price", 0.05, 1e6),
               "--time", _match(a.get("time"), HHMM, "time")]
        if cmd == "fill":
            out = _opt(a, "qty", lambda v: _num(v, "qty", 1, 100000, True), "--qty", out)
        else:
            reason = a.get("reason")
            if reason not in ("stop", "target", "flat_by", "no_progress", "manual"):
                raise SkillError("reason must be stop, target, flat_by, no_progress or manual")
            out += ["--reason", reason]
        return out
    return build


def _analyst_add(a):
    out = ["add", _sym(a.get("symbol")), "--quarter", _match(str(a.get("quarter", "")).upper(), QUARTER, "quarter")]
    for k in ("sales", "ebitda", "profit"):
        out += ["--" + k, _num(a.get(k), k, -1e9, 1e9)]
    out = _opt(a, "eps", lambda v: _num(v, "eps", -1e6, 1e6), "--eps", out)
    return _opt(a, "source", lambda v: _text(v, "source"), "--source", out)


def _tm_run(a):
    out = ["run", "--write"]
    out = _opt(a, "symbol", lambda v: _sym(v, True), "--symbol", out)
    out = _opt(a, "days", lambda v: _num(v, "days", 20, 100, True), "--days", out)
    return _opt(a, "stop", _stop, "--stop", out)


def _exit_plan(a):
    out = ["plan", "--write"]
    out = _opt(a, "stop", _stop, "--stop", out)
    return _opt(a, "entry_premium", lambda v: _num(v, "entry_premium", 0.05, 1e5), "--entry-premium", out)


# skill -> command -> (argv builder, the trader/ files it writes, a one-line description)
COMMANDS = {
    "in-entry": {"check": (_entry_check, ["entry.json"], "one look at the opening-range trigger for an index")},
    "in-scanner": {m: ((lambda m: lambda a: [m] + (["--stocks"] if a.get("stocks") else []))(m), ["watchlist.json"],
                       d) for m, d in (("orb", "opening-range breakout scan (after 09:30)"),
                                       ("daily", "daily setups: flags, oversold bounces"),
                                       ("universe", "the F&O universe for the scans"))},
    "in-news": {"run": (_news, ["news.json"], "headlines, events, the ban list and ASM/GSM flags")},
    "in-alerts": {"check": (lambda a: ["check"], [], "alerts on the trades you filled by hand (in-ticket)")},
    "in-ticket": {"show": (lambda a: ["show"] + ([_match(a["id"], TICKET, "ticket id")] if a.get("id") else []),
                           ["orders.json"], "today's tickets to type by hand"),
                  "fill": (_ticket_levels("fill"), ["orders.json"], "record the fill of a ticket"),
                  "close": (_ticket_levels("close"), ["orders.json"], "record the exit of a ticket"),
                  "skip": (lambda a: ["skip", _match(a.get("id"), TICKET, "ticket id")], ["orders.json"],
                           "a ticket you will not place"),
                  "journal": (lambda a: ["journal"], [], "today's trades, the year so far, the CSV")},
    "in-flows": {"inbox": (lambda a: ["inbox"], ["flows.json"], "load the CSVs in trader/flows/inbox/"),
                 "show": (lambda a: ["show"] + _opt(a, "days", lambda v: _num(v, "days", 1, 60, True), "--days", []),
                          [], "FII/DII flows, participant OI, bulk and block deals"),
                 "fetch": (lambda a: ["fetch"], ["flows.json"], "download config.json flows.urls, then load")},
    "in-analyst": {"add": (_analyst_add, ["analyst.json"], "store one quarter's numbers (Rs crore)"),
                   "show": (lambda a: ["show", _sym(a.get("symbol"))], [], "stored quarters, margins, growth"),
                   "checklist": (lambda a: ["checklist", _sym(a.get("symbol"))], [],
                                 "what to read in a results filing")},
    "in-risk": {"check": (lambda a: [] + (["--ticket", _match(a["ticket"], TICKET, "ticket")] if a.get("ticket")
                                         else []), ["orders.json"], "PASS / BLOCK every DRAFT ticket")},
    "in-exit": {"plan": (_exit_plan, ["plan.json"], "the exit plan for entry.json + pick.json"),
                "show": (lambda a: ["show"], [], "the saved plan.json")},
    "in-budget": {"size": (lambda a: ["--write"], ["orders.json"], "size plan.json into a DRAFT ticket")},
    "in-timemachine": {"run": (_tm_run, ["timemachine.json"], "replay the ORB rule on the last N days"),
                       "show": (lambda a: ["show"], [], "the saved replay")},
}


def catalog():
    return {k: {c: v[2] for c, v in cmds.items()} for k, cmds in COMMANDS.items()}


def argv_for(skill, command, args):
    try:
        build, outputs, _ = COMMANDS[skill][command]
    except KeyError:
        raise SkillError("unknown skill command %s %s" % (skill, command))
    return build(args or {}), outputs


def run(skill, command, args=None, trader=None, timeout=TIMEOUT_SEC):
    """Run one command to completion: the {"ok", "reason", "data"} document, also saved under state/skills/."""
    trader = trader or config.TRADER_DIR
    argv, outputs = argv_for(skill, command, args)
    python = getattr(sys, "_base_executable", None) or sys.executable
    cmd = [python, SCRIPTS[skill]] + argv + ["--trader", trader]
    started, t0 = smart.now_ist(), time.time()
    try:
        r = subprocess.run(cmd, cwd=str(config.BACKEND_DIR), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        code, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        code, out, err = -1, (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (
            e.stdout or ""), "timed out after %d s" % timeout
    lines = [x for x in (out or "").strip().splitlines() + (err or "").strip().splitlines() if x.strip()]
    files = {}
    for name in outputs:
        doc = smart.read_json(os.path.join(trader, name))
        if doc is not None:
            files[name] = doc
    doc = {"ok": code == 0, "reason": (lines[-1] if lines else "exit code %d" % code)[:500],
           "data": {"skill": skill, "command": command, "argv": argv, "exit_code": code, "stdout": out[-50000:],
                    "stderr": err[-10000:], "outputs": files, "started_ist": started.isoformat(),
                    "seconds": round(time.time() - t0, 2)}}
    folder = os.path.join(trader, "state", "skills")
    os.makedirs(folder, exist_ok=True)
    smart.write_json(os.path.join(folder, "%s_%s.json" % (skill, command)), doc)
    log.info("skill %s %s: exit %d in %.1f s", skill, command, code, doc["data"]["seconds"])
    return doc


def last(skill, command, trader=None):
    return smart.read_json(os.path.join(trader or config.TRADER_DIR, "state", "skills", "%s_%s.json" % (skill, command)))


class Background:
    """Runs a skill command in its own thread, at most one at a time per (skill, command): the engine's
    scheduled in-news and in-alerts runs never hold up a cycle."""

    def __init__(self):
        self.lock, self.busy = threading.Lock(), set()

    def start(self, skill, command, args=None, trader=None):
        key = (skill, command)
        with self.lock:
            if key in self.busy:
                return False
            self.busy.add(key)

        def go():
            try:
                run(skill, command, args, trader)
            except Exception:
                log.exception("skill %s %s failed", skill, command)
            finally:
                with self.lock:
                    self.busy.discard(key)
        threading.Thread(target=go, name="skill-%s-%s" % key, daemon=True).start()
        return True

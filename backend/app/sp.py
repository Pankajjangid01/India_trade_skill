"""The skill pack, imported in-process. Every module is standard library only."""

import importlib.util
import os
import sys

from app.config import SKILL_PACK_ROOT

sys.path.insert(0, os.path.join(SKILL_PACK_ROOT, "in-common"))
import smart  # noqa: E402,F401


def _load(name, skill, script):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SKILL_PACK_ROOT, skill, script))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chain = _load("sp_chain", "in-chain", "chain.py")       # the strike pick (delta, spread, liquidity, cost rules)
size = _load("sp_size", "in-budget", "size.py")         # lots from the max loss, charges included
firstrun = _load("sp_firstrun", "in-setup", "firstrun.py")
tm = _load("sp_timemachine", "in-timemachine", "timemachine.py")   # replay_day, stats_of, bootstrap (seed 19)
risk = _load("sp_risk", "in-risk", "check.py")         # PASS / BLOCK: verdict(ticket, context)
exitplan = _load("sp_exit", "in-exit", "exit.py")       # make_plan: premium stop, target, flat-by, no-progress
entry = _load("sp_entry", "in-entry", "entry.py")
scanner = _load("sp_scanner", "in-scanner", "scan.py")
news = _load("sp_news", "in-news", "news.py")
alerts = _load("sp_alerts", "in-alerts", "alerts.py")
ticket = _load("sp_ticket", "in-ticket", "ticket.py")
flows = _load("sp_flows", "in-flows", "flows.py")
analyst = _load("sp_analyst", "in-analyst", "facts.py")

# every skill's command-line script, for the skill runner (app/skills.py runs one-shot commands in a subprocess)
SCRIPTS = {"in-timemachine": "timemachine.py", "in-risk": "check.py", "in-exit": "exit.py", "in-entry": "entry.py",
           "in-scanner": "scan.py", "in-news": "news.py", "in-alerts": "alerts.py", "in-ticket": "ticket.py",
           "in-flows": "flows.py", "in-analyst": "facts.py", "in-chain": "chain.py", "in-budget": "size.py"}
SCRIPTS = {k: os.path.join(SKILL_PACK_ROOT, k, v) for k, v in SCRIPTS.items()}
FEED_SCRIPT = os.path.join(SKILL_PACK_ROOT, "in-common", "feed.py")
PROBE_SCRIPT = os.path.join(SKILL_PACK_ROOT, "in-setup", "firstrun.py")

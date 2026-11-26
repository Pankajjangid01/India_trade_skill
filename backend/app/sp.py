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
FEED_SCRIPT = os.path.join(SKILL_PACK_ROOT, "in-common", "feed.py")
PROBE_SCRIPT = os.path.join(SKILL_PACK_ROOT, "in-setup", "firstrun.py")

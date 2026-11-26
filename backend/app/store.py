"""Signals, paper trades and engine state in one SQLite file (trader/state/paper.sqlite).

signals.id is deterministic (date:strategy:symbol:bar:direction) and trades.signal_id
is UNIQUE, so one signal can never make two trades, across restarts too. History
is never deleted: "today" is a WHERE on the IST date.
"""

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY, date TEXT NOT NULL, strategy TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
  bar_start_ist TEXT, bar_close_ist TEXT, detected_at TEXT NOT NULL,
  entry_underlying REAL, stop_underlying REAL, target_underlying REAL, risk_points REAL, note TEXT,
  status TEXT NOT NULL, reason TEXT, trade_id TEXT, meta TEXT);
CREATE INDEX IF NOT EXISTS signals_date ON signals(date);
CREATE TABLE IF NOT EXISTS trades (
  id TEXT PRIMARY KEY, signal_id TEXT NOT NULL UNIQUE REFERENCES signals(id), date TEXT NOT NULL,
  strategy TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
  tradingsymbol TEXT, token TEXT, segment TEXT, instrument TEXT, lots INTEGER, quantity INTEGER, order_qtys TEXT,
  entry_price REAL NOT NULL, entry_time TEXT NOT NULL, entry_underlying REAL,
  stop_premium REAL, target_premium REAL, stop_underlying REAL, target_underlying REAL, flat_by TEXT,
  status TEXT NOT NULL, ltp REAL, ltp_time TEXT, underlying_ltp REAL,
  exit_price REAL, exit_time TEXT, exit_reason TEXT, gross_pnl REAL, charges REAL, net_pnl REAL,
  no_progress_bars INTEGER);
CREATE INDEX IF NOT EXISTS trades_date ON trades(date);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

TRADE_FIELDS = ("id", "signal_id", "date", "strategy", "symbol", "direction", "tradingsymbol", "token", "segment",
                "instrument", "lots", "quantity", "order_qtys", "entry_price", "entry_time", "entry_underlying",
                "stop_premium", "target_premium", "stop_underlying", "target_underlying", "flat_by", "status",
                "no_progress_bars")
EXECUTABLE = ("EXECUTED", "EXECUTABLE")


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(trades)")}
        if "no_progress_bars" not in cols:                 # a database from before in-exit's no-progress exit
            with self.db:
                self.db.execute("ALTER TABLE trades ADD COLUMN no_progress_bars INTEGER")

    def _rows(self, sql, args=()):
        with self.lock:
            return [self._dict(r) for r in self.db.execute(sql, args).fetchall()]

    @staticmethod
    def _dict(r):
        d = dict(r)
        for k in ("meta", "instrument", "order_qtys"):
            if d.get(k):
                d[k] = json.loads(d[k])
        return d

    # ---- kv
    def get(self, key, default=None):
        rows = self._rows("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def put(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))

    # ---- signals
    def has_signal(self, sid):
        return bool(self._rows("SELECT 1 FROM signals WHERE id = ?", (sid,)))

    def add_signal(self, sig, status, reason, detected_at, meta=None):
        """False when the id exists already (a duplicate is never re-recorded)."""
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO signals (id, date, strategy, symbol, direction, bar_start_ist, bar_close_ist, "
                "detected_at, entry_underlying, stop_underlying, target_underlying, risk_points, note, status, reason, "
                "meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sig["id"], sig["date"], sig["strategy"], sig["symbol"], sig["direction"], sig["bar_start_ist"],
                 sig["bar_close_ist"], detected_at, sig["entry_underlying"], sig["stop_underlying"],
                 sig["target_underlying"], sig["risk_points"], sig.get("note"), status, reason,
                 json.dumps(meta) if meta else None))
            return cur.rowcount == 1

    def set_signal(self, sid, status, reason=None, trade_id=None, meta=None):
        with self.lock, self.db:
            self.db.execute("UPDATE signals SET status = ?, reason = ?, trade_id = coalesce(?, trade_id), "
                            "meta = coalesce(?, meta) WHERE id = ?",
                            (status, reason, trade_id, json.dumps(meta) if meta else None, sid))

    def signals(self, date, limit=500):
        return self._rows("SELECT * FROM signals WHERE date = ? ORDER BY detected_at DESC, bar_close_ist DESC LIMIT ?",
                          (date, limit))

    # ---- trades
    def open_trade(self, t, signal_id):
        """Inserts the trade and marks its signal EXECUTED in one transaction."""
        row = dict(t, instrument=json.dumps(t["instrument"]), order_qtys=json.dumps(t["order_qtys"]))
        with self.lock, self.db:
            self.db.execute("INSERT INTO trades (%s) VALUES (%s)" % (", ".join(TRADE_FIELDS),
                                                                    ", ".join("?" * len(TRADE_FIELDS))),
                            [row.get(k) for k in TRADE_FIELDS])
            self.db.execute("UPDATE signals SET status = 'EXECUTED', reason = NULL, trade_id = ? WHERE id = ?",
                            (t["id"], signal_id))

    def mark(self, tid, ltp, ltp_time, underlying_ltp):
        with self.lock, self.db:
            self.db.execute("UPDATE trades SET ltp = coalesce(?, ltp), ltp_time = coalesce(?, ltp_time), "
                            "underlying_ltp = coalesce(?, underlying_ltp) WHERE id = ? AND status = 'OPEN'",
                            (ltp, ltp_time, underlying_ltp, tid))

    def close_trade(self, tid, price, when, reason, gross, charges, net):
        """True only for the call that actually closed it."""
        with self.lock, self.db:
            cur = self.db.execute("UPDATE trades SET status = 'CLOSED', exit_price = ?, exit_time = ?, exit_reason = ?, "
                                  "gross_pnl = ?, charges = ?, net_pnl = ?, ltp = ? WHERE id = ? AND status = 'OPEN'",
                                  (price, when, reason, gross, charges, net, price, tid))
            return cur.rowcount == 1

    def trade(self, tid):
        rows = self._rows("SELECT * FROM trades WHERE id = ?", (tid,))
        return rows[0] if rows else None

    def open_trades(self):
        return self._rows("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY entry_time")

    def trades(self, date=None, status=None, limit=500):
        sql, args = "SELECT * FROM trades WHERE 1 = 1", []
        if date:
            sql, args = sql + " AND date = ?", args + [date]
        if status:
            sql, args = sql + " AND status = ?", args + [status]
        return self._rows(sql + " ORDER BY entry_time DESC LIMIT ?", args + [limit])

    def count_trades(self, date, strategy=None):
        sql, args = "SELECT count(*) AS n FROM trades WHERE date = ?", [date]
        if strategy:
            sql, args = sql + " AND strategy = ?", args + [strategy]
        return self._rows(sql, args)[0]["n"]

    def next_trade_id(self, date):
        prefix = "P%s-" % date.replace("-", "")
        n = self._rows("SELECT count(*) AS n FROM trades WHERE id LIKE ?", (prefix + "%",))[0]["n"]
        return "%s%03d" % (prefix, n + 1)

    # ---- stats
    def stats(self, date=None, strategies=()):
        """{strategy: stats} for one IST date, or all time when date is None."""
        where, args = ("WHERE date = ?", (date,)) if date else ("", ())
        sig = {r["strategy"]: r for r in self._rows(
            "SELECT strategy, count(*) AS signals, sum(status IN %s) AS executable, sum(status = 'ENTRY_CUTOFF') AS "
            "cutoff, sum(status = 'REJECTED') AS rejected FROM signals %s GROUP BY strategy" % (str(EXECUTABLE), where),
            args)}
        tr = {r["strategy"]: r for r in self._rows(
            "SELECT strategy, count(*) AS trades, sum(status = 'OPEN') AS open, sum(status = 'CLOSED') AS completed, "
            "sum(status = 'CLOSED' AND net_pnl > 0) AS wins, sum(CASE WHEN status = 'CLOSED' THEN net_pnl END) AS "
            "net_pnl, sum(CASE WHEN status = 'CLOSED' THEN gross_pnl END) AS gross_pnl, "
            "sum(CASE WHEN status = 'CLOSED' THEN charges END) AS charges, "
            "max(CASE WHEN status = 'CLOSED' THEN net_pnl END) AS best, "
            "min(CASE WHEN status = 'CLOSED' THEN net_pnl END) AS worst FROM trades %s GROUP BY strategy" % where,
            args)}
        out = {}
        for k in list(strategies) + sorted((set(sig) | set(tr)) - set(strategies)):
            s, t = sig.get(k) or {}, tr.get(k) or {}
            completed, wins = t.get("completed") or 0, t.get("wins") or 0
            out[k] = {"signals": s.get("signals") or 0, "executable": s.get("executable") or 0,
                      "entry_cutoff": s.get("cutoff") or 0, "rejected": s.get("rejected") or 0,
                      "trades": t.get("trades") or 0, "open": t.get("open") or 0, "completed": completed,
                      "wins": wins, "losses": completed - wins,
                      "win_rate": round(wins * 100.0 / completed, 1) if completed else None,
                      "net_pnl": round(t.get("net_pnl") or 0, 2), "gross_pnl": round(t.get("gross_pnl") or 0, 2),
                      "charges": round(t.get("charges") or 0, 2), "best": t.get("best"), "worst": t.get("worst")}
        return out

    def daily(self, days=30):
        dates = [r["date"] for r in self._rows(
            "SELECT DISTINCT date FROM (SELECT date FROM signals UNION SELECT date FROM trades) "
            "ORDER BY date DESC LIMIT ?", (days,))]
        return [{"date": d, "by_strategy": self.stats(d)} for d in dates]

#!/usr/bin/env python3
"""in-common: the one door to Angel One SmartAPI for every in-* skill.

Standard library only. Read-only market data: this module has no order
function, and tests/test_repo.py fails the build if any file names an order
endpoint.

    clock     IST helpers, the pinnable --now, trading days, the market-data window
    files     atomic JSON writes and a cross-process lock file
    limiter   one REST budget for every process (trader/state/ratelimit.json)
    client    Client.call(): every REST request goes through it (retry, breaker)
    session   credentials from the environment or ./.env, TOTP, one login per
              IST day, token refresh
    master    the instrument master, cached once per IST day, and what it says
    candles   closed candles, cached per symbol, interval and day forever
    money     tick rounding, lots per order, option charges
    greeks    Black-Scholes price, Greeks and implied volatility
    stream    a minimal WebSocket client, the SmartStream tick parser, the
              feed's SQLite tick store and its readers, subscriptions

Skills import it by path, never from the current directory:

    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(HERE, "..", "in-common"))
    import smart

Endpoint paths, the master and stream URLs and the tick layout are taken from
Angel One's public SmartAPI documentation. Check each against your own copy;
in-setup's probe is what confirms them on your account.
"""

import base64
import collections
import datetime
import email.utils
import getpass
import hashlib
import hmac
import json
import math
import os
import random
import re
import socket
import sqlite3
import ssl
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

BASE = "https://apiconnect.angelone.in"
MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
STREAM_URL = "wss://smartapisocket.angelone.in/smart-stream"
PUBLIC_IP_URL = "https://api.ipify.org"

# name: (method, path, needs the day's session). Read-only market data only.
ENDPOINTS = {
    "loginByPassword": ("POST", "/rest/auth/angelbroking/user/v1/loginByPassword", False),
    "generateTokens": ("POST", "/rest/auth/angelbroking/jwt/v1/generateTokens", True),
    "getProfile": ("GET", "/rest/secure/angelbroking/user/v1/getProfile", True),
    "getCandleData": ("POST", "/rest/secure/angelbroking/historical/v1/getCandleData", True),
    "getOIData": ("POST", "/rest/secure/angelbroking/historical/v1/getOIData", True),
    "quote": ("POST", "/rest/secure/angelbroking/market/v1/quote/", True),
    "optionGreek": ("POST", "/rest/secure/angelbroking/marketData/v1/optionGreek", True),
    "putCallRatio": ("GET", "/rest/secure/angelbroking/marketData/v1/putCallRatio", True),
    "estimateCharges": ("POST", "/rest/secure/angelbroking/brokerage/v1/estimateCharges", True),
    "OIBuildup": ("POST", "/rest/secure/angelbroking/marketData/v1/OIBuildup", True),
    "gainersLosers": ("POST", "/rest/secure/angelbroking/marketData/v1/gainersLosers", True),
    "nseIntraday": ("GET", "/rest/secure/angelbroking/marketData/v1/nseIntraday", True),
    "bseIntraday": ("GET", "/rest/secure/angelbroking/marketData/v1/bseIntraday", True),
    "cautionaryScrips": ("GET", "/rest/secure/angelbroking/securities/v1/cautionaryScrips", True),  # doc: GET with {"scripconsent": "yes"}
}

# (calls, window in seconds): about 70% of the documented limits (BRD section 4)
DESIGN_CAPS = {
    "loginByPassword": ((1, 1),),
    "generateTokens": ((1, 1), (700, 3600)),
    "estimateCharges": ((1, 1),),
    "getProfile": ((2, 1), (700, 3600)),
    "getCandleData": ((1, 1), (28, 60), (1400, 3600)),
    "getOIData": ((1, 2),),
    "quote": ((1, 1),),
    "optionGreek": ((1, 2),),
    "putCallRatio": ((1, 1),),
}
DEFAULT_CAP = ((1, 1),)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
CREDENTIALS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN")


class SmartError(Exception):
    """One plain line for the user: what went wrong and the next step."""


# ---------------------------------------------------------------- clock

def parse_ist(text):
    """'2026-11-24 10:20' (IST) or an ISO time with an offset -> aware IST datetime."""
    text = text.strip()
    # Python 3.9's fromisoformat takes only 3 or 6 fraction digits: pad or cut any other count to 6
    text = re.sub(r"(T|\s)(\d{2}:\d{2}:\d{2})\.(\d+)", lambda m: "%s%s.%s" % (
        m.group(1), m.group(2), (m.group(3) + "000000")[:6]), text, count=1)
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise SmartError('a time wants "YYYY-MM-DD HH:MM" in IST, got %r' % text)
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)


def now_ist(pinned=None):
    return parse_ist(pinned) if pinned else datetime.datetime.now(IST).replace(microsecond=0)


def ist_date(epoch):
    return datetime.datetime.fromtimestamp(epoch, IST).date().isoformat()


def trading_day(day, calendar=None):
    """A weekday that calendar.json does not list as a holiday, or a listed special session."""
    calendar = calendar or {}
    iso = day.isoformat()
    if iso in (calendar.get("session_overrides") or {}):
        return True
    return day.weekday() < 5 and iso not in (calendar.get("holidays") or {})


def previous_trading_day(day, calendar=None):
    day -= datetime.timedelta(days=1)
    while not trading_day(day, calendar):
        day -= datetime.timedelta(days=1)
    return day


def market_data_allowed(now, calendar=None, start="09:00", stop="15:45"):
    """(allowed, reason). Live market-data calls only on trading days, 09:00 to 15:45 IST.
    The instrument master and history are exempt; callers decide which call is which."""
    calendar = calendar or {}
    # ponytail: an override day uses the normal window; a Muhurat evening session needs its own hours here
    if not trading_day(now.date(), calendar):
        holiday = (calendar.get("holidays") or {}).get(now.date().isoformat())
        return False, "closed: holiday (%s)" % holiday if holiday else "closed: weekend"
    hhmm = now.strftime("%H:%M")
    if not start <= hhmm < stop:
        return False, "closed: outside %s to %s IST" % (start, stop)
    return True, "open"


# ---------------------------------------------------------------- files

def write_bytes(path, data):
    """Atomic: a temp file in the same folder, then a rename. mkstemp makes it
    owner-only on macOS and Linux; on Windows the user folder's ACL applies."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_json(path, obj):
    write_bytes(path, (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


class FileLock:
    """Cross-process lock: whoever creates the file holds it. A lock older than
    `stale` seconds was left by a crashed process and is broken."""

    def __init__(self, path, stale=10.0):
        self.path, self.stale = path, stale

    def __enter__(self):
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except (FileExistsError, PermissionError):  # Windows says PermissionError mid-delete
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                time.sleep(0.01)

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass


# ---------------------------------------------------------------- limiter

def caps_from_config(limits):
    """DESIGN_CAPS with the config's "limits" block applied."""
    caps = dict(DESIGN_CAPS)
    if limits:
        caps["getCandleData"] = ((limits.get("candle_per_sec", 1), 1), (limits.get("candle_per_min", 28), 60),
                                 (limits.get("candle_per_hour", 1400), 3600))
        caps["quote"] = ((limits.get("quote_per_sec", 1), 1),)
        caps["optionGreek"] = ((1, limits.get("greeks_min_gap_sec", 2)),)
    return caps


def _cap_text(caps):
    unit = {1: "s", 60: "min", 3600: "h"}
    return ", ".join("%d/%s" % (n, unit.get(w, "%ds" % w)) for n, w in caps)


class Limiter:
    """One REST budget for every process. Call times live in state/ratelimit.json,
    read and written under a lock file, so skills running at once share it."""

    def __init__(self, state_dir, caps=None, clock=time.time, sleep=time.sleep):
        self.path = os.path.join(state_dir, "ratelimit.json")
        self.lock = self.path + ".lock"
        self.caps = caps or DESIGN_CAPS
        self.clock, self.sleep = clock, sleep

    def wait(self, name, caps=None):
        """Block until `name` fits under every cap, then book the call."""
        caps = caps or self.caps.get(name, DEFAULT_CAP)
        while True:
            with FileLock(self.lock):
                data = read_json(self.path, {})
                now = self.clock()
                calls = data.setdefault("calls", {})
                recent = [t for t in calls.get(name, []) if now - t < 3600]
                delay = 0.0
                for n, window in caps:
                    inside = [t for t in recent if now - t < window]
                    if len(inside) >= n:
                        delay = max(delay, inside[-n] + window - now)
                if delay <= 0:
                    calls[name] = recent + [now]
                    usage = data.get("usage") or {}
                    if usage.get("date") != ist_date(now):
                        usage = {"date": ist_date(now), "counts": {}}
                    usage["counts"][name] = usage["counts"].get(name, 0) + 1
                    data["usage"] = usage
                    write_json(self.path, data)
                    return
            self.sleep(delay)

    def usage_line(self):
        data = read_json(self.path, {})
        now = self.clock()
        usage = data.get("usage") or {}
        if usage.get("date") != ist_date(now) or not usage.get("counts"):
            return "API usage today: no calls"
        parts = []
        for name in sorted(usage["counts"]):
            hour = sum(1 for t in data.get("calls", {}).get(name, []) if now - t < 3600)
            parts.append("%s %d today, %d this hour (cap %s)"
                         % (name, usage["counts"][name], hour, _cap_text(self.caps.get(name, DEFAULT_CAP))))
        return "API usage: " + "; ".join(parts)


# ---------------------------------------------------------------- client

def http(method, url, headers=None, data=None, timeout=20):
    """(status, headers, body bytes). An HTTP error is a reply, not an exception."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except (urllib.error.URLError, OSError) as e:
        raise SmartError("cannot reach %s: %s" % (urllib.parse.urlsplit(url).hostname,
                                                  getattr(e, "reason", e)))


class Replay:
    """Offline transport: DIR/<endpoint>.json holds one {"status", "headers", "body"}
    or a list of them, replayed in order (the last one repeats)."""

    def __init__(self, folder):
        self.folder = folder
        self.by_path = {path: name for name, (_, path, _) in ENDPOINTS.items()}
        self.seen = collections.Counter()

    def __call__(self, method, url, headers=None, data=None):
        name = self.by_path.get(urllib.parse.urlsplit(url).path)
        rec = read_json(os.path.join(self.folder, "%s.json" % name))
        if rec is None:
            raise SmartError("offline: no %s.json in %s" % (name, self.folder))
        if isinstance(rec, list):
            rec = rec[min(self.seen[name], len(rec) - 1)]
        self.seen[name] += 1
        return rec["status"], rec.get("headers") or {}, json.dumps(rec["body"]).encode("utf-8")


SECRET_FIELDS = {"jwtToken", "refreshToken", "feedToken", "password", "totp", "clientcode"}
PRIVATE_ENDPOINTS = {"getProfile"}  # every value in it is personal


def redact(obj, everything=False):
    """Secret fields (and, with everything, every value) become "REDACTED"; the shape stays."""
    if isinstance(obj, dict):
        return {k: redact(v, everything or k in SECRET_FIELDS) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, everything) for v in obj]
    return "REDACTED" if everything and obj not in (None, "") else obj


class Reply:
    def __init__(self, name, status, headers, body, ms, received):
        self.name, self.status, self.headers, self.body = name, status, headers, body
        self.ms, self.received = ms, received

    @property
    def ok(self):
        return self.status == 200 and isinstance(self.body, dict) and self.body.get("status") is True

    @property
    def data(self):
        return self.body.get("data") if isinstance(self.body, dict) else None

    @property
    def errorcode(self):
        return (self.body.get("errorcode") or None) if isinstance(self.body, dict) else None

    @property
    def message(self):
        return str(self.body.get("message", "")) if isinstance(self.body, dict) else ""

    def header(self, name):
        return next((v for k, v in self.headers.items() if k.lower() == name.lower()), None)

    def summary(self):
        out = {"ok": self.ok, "http": self.status, "ms": self.ms}
        if not self.ok:
            out.update(errorcode=self.errorcode, message=self.message[:200])
        return out

    def why(self):
        return "HTTP %s %s %s" % (self.status, self.errorcode or "", self.message[:200])


RETRY_WAITS = (2, 4, 8)            # seconds, plus up to 1 s of jitter
RETRY_CODES = {"AB2001"}           # "Internal Error, Please try after sometime"
AUTH_CODES = {"AG8001", "AG8002", "AG8003", "AB8050", "AB8051"}
BREAKER_FAILS, BREAKER_SEC = 3, 60


class Client:
    """Every REST request goes through call(): limiter first, then the transport.
    It knows only the read-only ENDPOINTS above.

    call() retries only HTTP 403 (the rate-limit reply), 5xx and AB2001, waiting
    2, 4 and 8 s plus jitter, then hands back the last failure for the caller to
    report as partial. A token error gets one recovery through the Session. Three
    failures in a row pause that endpoint for 60 s (per process)."""

    def __init__(self, api_key, limiter=None, transport=None, record=None, ips=None, clock=time.time,
                 sleep=time.sleep, rand=random.random):
        self.api_key, self.limiter, self.record, self.clock = api_key, limiter, record, clock
        self.sleep, self.rand = sleep, rand
        self.transport = transport or http
        self.ips = ips or {"local": "127.0.0.1", "public": "127.0.0.1", "mac": "00:00:00:00:00:00"}
        self.jwt = None
        self.reauth = None   # set by Session: errorcode -> True when there is a new token to retry with
        self.recorded = collections.defaultdict(list)
        self.fails = collections.Counter()
        self.paused_until = {}

    def headers(self, auth):
        h = {"Content-Type": "application/json", "Accept": "application/json",
             "X-UserType": "USER", "X-SourceID": "WEB", "X-PrivateKey": self.api_key,
             "X-ClientLocalIP": self.ips["local"], "X-ClientPublicIP": self.ips["public"],
             "X-MACAddress": self.ips["mac"]}
        if auth:
            h["Authorization"] = "Bearer " + self.jwt
        return h

    def call(self, name, body=None, caps=None, retry=True):
        """One logical request. retry=False sends it once and reports exactly what came back
        (the probe uses that to measure)."""
        if self.clock() < self.paused_until.get(name, 0):
            return Reply(name, 0, {}, {"status": False, "errorcode": "PAUSED", "message": "%s paused for %d s after %d "
                                       "failures in a row" % (name, BREAKER_SEC, BREAKER_FAILS)}, 0, self.clock())
        reauthed = False
        attempt = 0
        while True:
            try:
                r = self._once(name, body, caps)
            except SmartError:
                self._failed(name)
                raise
            if r.ok:
                break
            if r.errorcode in AUTH_CODES and self.reauth and not reauthed:
                reauthed = True
                if self.reauth(r.errorcode):
                    continue
                break
            if not retry:
                break
            if not (r.status == 403 or (r.status or 0) >= 500 or r.errorcode in RETRY_CODES) \
                    or attempt == len(RETRY_WAITS):
                break
            self.sleep(RETRY_WAITS[attempt] + self.rand())
            attempt += 1
        if r.ok:
            self.fails[name] = 0
        elif retry:
            self._failed(name)
        return r

    def _failed(self, name):
        self.fails[name] += 1
        if self.fails[name] >= BREAKER_FAILS:
            self.paused_until[name] = self.clock() + BREAKER_SEC
            self.fails[name] = 0

    def _once(self, name, body=None, caps=None):
        method, path, auth = ENDPOINTS[name]
        if auth and not self.jwt:
            raise SmartError("not logged in: run in-setup login first")
        if self.limiter:
            self.limiter.wait(name, caps)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        t0 = self.clock()
        status, headers, raw = self.transport(method, BASE + path, self.headers(auth), data)
        t1 = self.clock()
        try:
            parsed = json.loads(raw.decode("utf-8") or "null")
        except ValueError:
            parsed = {"status": False, "message": raw[:200].decode("utf-8", "replace")}
        reply = Reply(name, status, headers, parsed, int(round((t1 - t0) * 1000)), (t0 + t1) / 2)
        if self.record:  # redacted, in call order, ready to be an offline fixture
            body = redact(parsed)
            if name in PRIVATE_ENDPOINTS and isinstance(body, dict) and "data" in body:
                body["data"] = redact(body["data"], True)
            self.recorded[name].append({"status": status, "headers": {"Date": reply.header("Date")}, "body": body})
            write_json(os.path.join(self.record, name + ".json"), self.recorded[name])
        return reply


def local_ip():
    try:  # a UDP connect picks the outgoing interface and sends nothing
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def mac_address():
    n = uuid.getnode()
    return ":".join("%02x" % ((n >> shift) & 0xFF) for shift in range(40, -1, -8))


def public_ip(setting, state_dir, today, transport=None):
    """The config's api.client_public_ip, or ("auto") looked up once per IST day."""
    if setting and setting != "auto":
        return setting
    path = os.path.join(state_dir, "public_ip.json")
    cached = read_json(path, {})
    if cached.get("date") == today and cached.get("ip"):
        return cached["ip"]
    status, _, raw = (transport or http)("GET", PUBLIC_IP_URL, {}, None)
    ip = raw.decode("ascii", "replace").strip()
    if status != 200 or not re.match(r"^[0-9a-fA-F:.]+$", ip):
        raise SmartError("could not look up your public IP: set api.client_public_ip in trader/config.json")
    write_json(path, {"date": today, "ip": ip})
    return ip


# ---------------------------------------------------------------- session

def read_env_file(path):
    """KEY=VALUE lines of a .env file (comments, blank lines, `export ` and quotes allowed)."""
    out = {}
    try:
        fh = open(path, encoding="utf-8-sig")
    except (FileNotFoundError, IsADirectoryError):
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            out[k] = v
    return out


def credentials(env=None, required=True, env_file=".env"):
    """From the environment, then ANGEL_* lines of ./.env (the folder you run from,
    outside any repo). Never printed. Offline runs (required=False) fall back to placeholders."""
    env = dict(os.environ if env is None else env)
    for k, v in read_env_file(env_file).items():
        if k.startswith("ANGEL_") and v:
            env.setdefault(k, v)  # the environment wins over the file
    missing = [k for k in CREDENTIALS if not env.get(k)]
    if missing and required:
        raise SmartError("set %s in the environment or in a .env file in this folder "
                         "(never in chat or in a file in the repo)" % ", ".join(missing))
    return {"api_key": env.get("ANGEL_API_KEY") or "OFFLINE",
            "client_code": env.get("ANGEL_CLIENT_CODE") or "OFFLINE",
            "pin": env.get("ANGEL_PIN") or "0000",
            "totp_secret": env.get("ANGEL_TOTP_SECRET") or (None if required else "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")}


def totp(secret_b32, at=None, digits=6, step=30):
    """RFC 6238 TOTP (SHA-1), the code an authenticator app shows."""
    secret = secret_b32.replace(" ", "").upper()
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    return str((struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % 10 ** digits).zfill(digits)


def totp_code(creds, ask=None, at=None):
    if creds.get("totp_secret"):
        return totp(creds["totp_secret"], at)
    if ask is None:
        if not sys.stdin.isatty():
            raise SmartError("no ANGEL_TOTP_SECRET: run login in a terminal so it can ask for the 6-digit code")
        ask = getpass.getpass
    code = ask("6-digit code from your authenticator app: ").strip()
    if not re.match(r"^\d{6}$", code):
        raise SmartError("the TOTP code is 6 digits")
    return code


def key_fingerprint(api_key):
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]


def login(client, creds, path, today, ask=None, force=False):
    """One login per IST day per API key, kept in `path` (state/session.json).
    Returns (session, login Reply or None when reused). One attempt only: a
    failed login is reported, never retried in a loop."""
    # ponytail: no token refresh yet (AG8002 / AG8001 / AB8051); Phase 1 adds it in this function
    fp = key_fingerprint(creds["api_key"])
    s = read_json(path, {})
    if not force and s.get("date_ist") == today and s.get("key") == fp and s.get("jwt"):
        client.jwt = s["jwt"]
        return s, None
    reply = client.call("loginByPassword", {"clientcode": creds["client_code"], "password": creds["pin"],
                                            "totp": totp_code(creds, ask, client.clock())})
    if not reply.ok:
        raise SmartError("login failed (%s): check the PIN, the TOTP and the API key" % reply.why().strip())
    d = reply.data or {}
    jwt = d.get("jwtToken") or ""
    s = {"date_ist": today, "key": fp, "jwt": _strip_bearer(jwt),
         "refresh": d.get("refreshToken"), "feed": d.get("feedToken")}
    write_json(path, s)
    client.jwt = s["jwt"]
    return s, reply


def _strip_bearer(jwt):
    return jwt[7:] if jwt and jwt.startswith("Bearer ") else jwt


class Session:
    """The day's login for one API key, shared through state/session.json.

    Token errors get one recovery per call: first the file is re-read (another
    process may have refreshed it); AG8002 and AG8001 then try generateTokens;
    whatever is left (AG8001 again, AB8050, AB8051) gets a fresh login, at most
    once per process. Never a loop."""

    def __init__(self, client, creds, path, today, ask=None):
        self.client, self.creds, self.path, self.today, self.ask = client, creds, path, today, ask
        self.data, self.relogged = {}, False
        client.reauth = self.recover

    def start(self):
        """Today's session (reused or new). Returns the login Reply, or None when reused."""
        self.data, reply = login(self.client, self.creds, self.path, self.today, self.ask)
        return reply

    def recover(self, code):
        disk = read_json(self.path, {}) or {}
        if disk.get("jwt") and disk.get("jwt") != self.client.jwt and disk.get("key") == self.data.get("key"):
            self.data, self.client.jwt = disk, disk["jwt"]
            return True
        if code in ("AG8001", "AG8002") and self.data.get("refresh"):
            r = self.client._once("generateTokens", {"refreshToken": self.data["refresh"]})
            if r.ok:
                d = r.data or {}
                self.data = dict(self.data, jwt=_strip_bearer(d.get("jwtToken") or ""),
                                 refresh=d.get("refreshToken") or self.data["refresh"],
                                 feed=d.get("feedToken") or self.data.get("feed"))
                write_json(self.path, self.data)
                self.client.jwt = self.data["jwt"]
                return True
        if self.relogged:
            return False
        self.relogged = True
        self.data, _ = login(self.client, self.creds, self.path, self.today, self.ask, force=True)
        return True


def clock_check(reply):
    """Server time from a reply's Date header against this computer, at the request midpoint."""
    date = reply.header("Date")
    if not date:
        return {"ok": None, "note": "no Date header in the reply"}
    server = email.utils.parsedate_to_datetime(date).timestamp() + 0.5  # Date is whole seconds: take the middle
    offset = round(server - reply.received, 1)
    return {"offset_sec": offset, "resolution_sec": 1, "ok": abs(offset) <= 2.0,
            "basis": "HTTP Date header of %s" % reply.name}


# ---------------------------------------------------------------- master

EXPECTED_FIELDS = ("token", "symbol", "name", "expiry", "strike", "lotsize", "instrumenttype",
                   "exch_seg", "tick_size", "freeze_qty", "is_cas_enabled")
SEGMENTS = {"NSE": "NSE", "NFO": "NFO", "BSE": "BSE", "BFO": "BFO",
            "NSE_CM": "NSE", "NSE_FO": "NFO", "BSE_CM": "BSE", "BSE_FO": "BFO",
            "CDS": "CDS", "MCX": "MCX", "NCDEX": "NCDEX", "NCO": "NCO"}  # the last four: in the master, unused here


def segment(label):
    return SEGMENTS.get((label or "").upper(), label)


def expiry_date(text):
    try:
        return datetime.datetime.strptime(text, "%d%b%Y").date()
    except (TypeError, ValueError):
        return None


def load_master(cache_dir, today, path=None, transport=None):
    """Rows of OpenAPIScripMaster.json: a given file, or today's cached copy,
    downloaded once per IST day (yesterday's copy is deleted)."""
    if path is None:
        path = os.path.join(cache_dir, "instrument_master_%s.json" % today)
        if not os.path.exists(path):
            status, _, raw = (transport or http)("GET", MASTER_URL, {}, None)
            if status != 200:
                raise SmartError("instrument master download failed: HTTP %s" % status)
            rows = json.loads(raw.decode("utf-8"))
            write_bytes(path, raw)
            for f in os.listdir(cache_dir):
                if f.startswith("instrument_master_") and f != os.path.basename(path):
                    os.remove(os.path.join(cache_dir, f))
            return rows
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _num(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


SCALE_CANDIDATES = (1, 10, 100, 1000, 10000)


def _plain(x):
    return ("%.4f" % x).rstrip("0").rstrip(".")


def strike_scale(rows):
    """The scale between the raw strike and the strike written at the end of the
    trading symbol, row by row, without parsing the date part: NFO writes
    NIFTY27JUN2830000CE, BFO writes SENSEX26NOV80700CE (monthly) and
    SENSEX26D0370600PE (weekly). Returns (most common scale, rows that fit it
    and only it, option rows checked)."""
    fits, checked = collections.Counter(), 0
    for r in rows:
        sym, raw = r.get("symbol") or "", _num(r.get("strike"))
        if r.get("instrumenttype") not in ("OPTIDX", "OPTSTK") or sym[-2:] not in ("CE", "PE") or not raw:
            continue
        checked += 1
        match = [c for c in SCALE_CANDIDATES if sym[:-2].endswith(_plain(raw / c))]
        if len(match) == 1:
            fits[match[0]] += 1
    if not fits:
        return None, 0, checked
    scale, agree = fits.most_common(1)[0]
    return scale, agree, checked


def index_options(rows, name, today):
    """{expiry date: rows} for an index's options expiring today or later."""
    out = collections.defaultdict(list)
    for r in rows:
        if r.get("instrumenttype") == "OPTIDX" and r.get("name") == name:
            d = expiry_date(r.get("expiry"))
            if d and d >= today:
                out[d].append(r)
    return out


def underlying_facts(rows, name, today, scale):
    index = next((r for r in rows if r.get("instrumenttype") == "AMXIDX" and r.get("name") == name), None)
    by_exp = index_options(rows, name, today)
    exps = sorted(by_exp)
    f = {"index_token": index.get("token") if index else None,
         "index_symbol": index.get("symbol") if index else None,
         "index_segment": segment(index.get("exch_seg")) if index else None,
         "expiries": [[d.isoformat(), WEEKDAYS[d.weekday()]] for d in exps[:4]]}
    if exps:
        near = by_exp[exps[0]]
        lots = collections.Counter(int(float(r.get("lotsize") or 0)) for r in near)
        strikes = sorted({_num(r.get("strike")) / scale for r in near if _num(r.get("strike"))}) if scale else []
        steps = collections.Counter(round(b - a, 2) for a, b in zip(strikes, strikes[1:]))
        freeze = collections.Counter(r.get("freeze_qty") for r in near if r.get("freeze_qty"))
        f.update(segment=segment(near[0].get("exch_seg")), lot_size=lots.most_common(1)[0][0],
                 strike_step=steps.most_common(1)[0][0] if steps else None,
                 contracts_nearest=len(near),
                 freeze_qty=int(float(freeze.most_common(1)[0][0])) if freeze else None)
        if len(lots) > 1:
            f["lot_sizes_seen"] = sorted(lots)
    return f


def describe_master(rows, today, underlyings=("NIFTY", "BANKNIFTY", "SENSEX")):
    """What the master says about itself: fields, labels, scales, lots, expiries.
    Nothing here is assumed; every number is counted from the rows."""
    fields = set()
    for r in rows[:5000]:
        fields.update(r)
    segs = collections.Counter(r.get("exch_seg") for r in rows)
    types = collections.Counter(r.get("instrumenttype") for r in rows)
    ticks = collections.Counter(r.get("tick_size") for r in rows if r.get("instrumenttype") == "OPTIDX")
    scale, agree, checked = strike_scale(rows)
    return {"rows": len(rows),
            "fields_missing": [f for f in EXPECTED_FIELDS if f not in fields],
            "exch_seg": dict(segs.most_common()),
            "unknown_segments": sorted(s for s in segs if s and s.upper() not in SEGMENTS),
            "instrumenttype": dict(types.most_common()),
            "strike_scale": scale, "strike_scale_agree": [agree, checked],
            "option_tick_size_raw": dict(ticks.most_common(3)),
            "tick_size_scale": scale, "tick_size_basis": "inferred: same scale as the strike, not measured",
            "underlyings": {u: underlying_facts(rows, u, today, scale) for u in underlyings}}


def nearest_option(rows, name, spot, scale, today, kind="CE"):
    """The nearest-expiry option of an index closest to spot (the middle strike when spot is unknown)."""
    by_exp = index_options(rows, name, today)
    if not by_exp or not scale:
        return None
    near = sorted((r for r in by_exp[min(by_exp)] if (r.get("symbol") or "").endswith(kind)),
                  key=lambda r: _num(r.get("strike")) or 0)
    if not near:
        return None
    if spot is None:
        return near[len(near) // 2]
    return min(near, key=lambda r: abs((_num(r.get("strike")) or 0) / scale - spot))


# ---------------------------------------------------------------- stream

EXCHANGE_TYPES = {1: "nse_cm", 2: "nse_fo", 3: "bse_cm", 4: "bse_fo", 5: "mcx_fo", 7: "ncx_fo", 13: "cde_fo"}
SEGMENT_CODES = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}
MODES = {"ltp": 1, "quote": 2, "snapquote": 3}


def parse_tick(b):
    """One SmartStream binary packet (little-endian) -> dict. Prices arrive in paise."""
    if len(b) < 51:
        raise ValueError("short packet: %d bytes" % len(b))
    q = lambda off: struct.unpack_from("<q", b, off)[0]  # noqa: E731
    tick = {"mode": b[0], "exchange_type": b[1],
            "token": b[2:27].split(b"\0", 1)[0].decode("ascii", "replace"),
            "seq": q(27), "exch_ms": q(35), "ltp": q(43) / 100.0}
    if b[0] >= 2 and len(b) >= 123:
        tick.update(volume=q(67), open=q(91) / 100.0, high=q(99) / 100.0, low=q(107) / 100.0,
                    close=q(115) / 100.0)
    if b[0] == 3 and len(b) >= 379:
        tick.update(oi=q(131), upper_circuit=q(347) / 100.0, lower_circuit=q(355) / 100.0)
    return tick


class StreamRefused(SmartError):
    """The stream answered but did not accept the connection (usually the token)."""


class WebSocket:
    """Just enough RFC 6455 for one client: TLS, handshake, masked sends,
    fragmented receives, ping answered with pong."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    @classmethod
    def connect(cls, url, headers, timeout=10):
        u = urllib.parse.urlsplit(url)
        try:
            raw = socket.create_connection((u.hostname, u.port or 443), timeout=timeout)
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
        except OSError as e:
            raise SmartError("cannot reach %s: %s" % (u.hostname, e))
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        lines = ["GET %s HTTP/1.1" % (u.path or "/"), "Host: %s" % u.hostname, "Upgrade: websocket",
                 "Connection: Upgrade", "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
        lines += ["%s: %s" % kv for kv in headers.items()]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        ws = cls(sock)
        while b"\r\n\r\n" not in ws.buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            ws.buf += chunk
        head, _, ws.buf = ws.buf.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status + " ":
            sock.close()
            raise StreamRefused("stream refused the connection: %s" % (status or "no reply"))
        accept = base64.b64encode(hashlib.sha1((key + cls.GUID).encode("ascii")).digest())
        if accept not in head:
            sock.close()
            raise SmartError("stream handshake failed: bad Sec-WebSocket-Accept")
        return ws

    def send(self, payload, opcode=1):
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        n = len(payload)
        if n < 126:
            head = struct.pack(">BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            head = struct.pack(">BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | opcode, 0x80 | 127, n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def _frame(self):
        """(fin, opcode, payload) from the buffer, or None until a whole frame is in."""
        b = self.buf
        if len(b) < 2:
            return None
        n, i = b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, i = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, i = struct.unpack(">Q", b[2:10])[0], 10
        mask = None
        if b[1] & 0x80:
            mask, i = b[i:i + 4], i + 4
        if len(b) < i + n:
            return None
        data = b[i:i + n]
        if mask:
            data = bytes(c ^ mask[k % 4] for k, c in enumerate(data))
        self.buf = b[i + n:]
        return bool(b[0] & 0x80), b[0] & 0x0F, data

    def recv(self, timeout=None):
        """Next message as (opcode, payload, received epoch ms): 1 text, 2 binary, 8 closed.
        Raises socket.timeout when nothing arrives in time."""
        self.sock.settimeout(timeout)
        parts, op = [], None
        while True:
            f = self._frame()
            if f is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    return 8, b"", int(time.time() * 1000)
                self.buf += chunk
                continue
            fin, code, data = f
            if code == 9:
                self.send(data, 10)
                continue
            if code == 10:
                continue
            if code == 8:
                return 8, data, int(time.time() * 1000)
            if code:
                op = code
            parts.append(data)
            if fin:
                return op, b"".join(parts), int(time.time() * 1000)

    def close(self):
        try:
            self.send(b"", 8)
            self.sock.close()
        except OSError:
            pass


class ReplayStream:
    """Offline stream: a JSON-lines file (or a list) of {"ms", "op", "text" or "hex"}.
    When the frames run out it times out, or with end_with_close it closes, like a
    dropped connection. What was sent is kept in .sent for tests."""

    def __init__(self, path_or_frames, end_with_close=False):
        if isinstance(path_or_frames, list):
            self.frames = list(path_or_frames)
        else:
            with open(path_or_frames, encoding="utf-8") as fh:
                self.frames = [json.loads(line) for line in fh if line.strip()]
        self.end_with_close, self.sent, self.last_ms = end_with_close, [], 0

    def send(self, payload, opcode=1):
        self.sent.append(payload)

    def recv(self, timeout=None):
        if not self.frames:
            if self.end_with_close:
                return 8, b"", self.last_ms
            raise socket.timeout("offline stream: no more frames")
        f = self.frames.pop(0)
        data = f["text"].encode("utf-8") if "text" in f else bytes.fromhex(f["hex"])
        self.last_ms = f["ms"]
        return f["op"], data, f["ms"]

    def close(self):
        pass


def open_stream(session, creds, url=STREAM_URL):
    """Connect to SmartStream with the day's session. The documentation copy has no
    stream section, so the Authorization form is tried both ways: Bearer, then bare.
    Returns (connection, which form worked). StreamRefused when both are refused."""
    last = None
    for name, auth in (("Bearer jwt", "Bearer " + session["jwt"]), ("bare jwt", session["jwt"])):
        try:
            return WebSocket.connect(url, {"Authorization": auth, "x-api-key": creds["api_key"],
                                           "x-client-code": creds["client_code"],
                                           "x-feed-token": session.get("feed") or ""}), name
        except StreamRefused as e:
            last = e
    raise last


# ---------------------------------------------------------------- tick store

TICKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS latest (
    token TEXT PRIMARY KEY, exchange_type INTEGER, exch_ms INTEGER, recv_ms INTEGER,
    ltp REAL, cum_volume INTEGER, oi INTEGER, upper_circuit REAL, lower_circuit REAL);
CREATE TABLE IF NOT EXISTS bars_1m (
    token TEXT, start_ms INTEGER, open REAL, high REAL, low REAL, close REAL,
    cum_volume INTEGER, oi INTEGER, ticks INTEGER, PRIMARY KEY (token, start_ms));
"""


class TickStore:
    """The feed's SQLite file: the latest tick per token and 1-minute bars on the
    exchange clock. One writer (the feed), any number of readers (WAL mode).
    Ticks are buffered in memory and written by flush(), about once a second."""
    # ponytail: no raw tick history, only latest + 1-minute bars; add a ticks table if raw replay is ever needed

    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(TICKS_SCHEMA)
        self.latest, self.bars = {}, {}

    def add(self, tick, recv_ms):
        token, px, vol, oi = tick["token"], tick["ltp"], tick.get("volume"), tick.get("oi")
        self.latest[token] = (token, tick["exchange_type"], tick["exch_ms"], recv_ms, px, vol, oi,
                              tick.get("upper_circuit"), tick.get("lower_circuit"))
        key = (token, tick["exch_ms"] - tick["exch_ms"] % 60000)
        b = self.bars.get(key)
        if b is None:
            self.bars[key] = [px, px, px, px, vol, oi, 1]
        else:
            b[1], b[2], b[3] = max(b[1], px), min(b[2], px), px
            b[4] = vol if vol is not None else b[4]
            b[5] = oi if oi is not None else b[5]
            b[6] += 1

    def flush(self):
        if not self.latest and not self.bars:
            return
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO latest VALUES (?,?,?,?,?,?,?,?,?)", list(self.latest.values()))
            self.db.executemany(
                "INSERT INTO bars_1m VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(token, start_ms) DO UPDATE SET "
                "high = max(high, excluded.high), low = min(low, excluded.low), close = excluded.close, "
                "cum_volume = coalesce(excluded.cum_volume, cum_volume), oi = coalesce(excluded.oi, oi), "
                "ticks = ticks + excluded.ticks",
                [(t, start) + tuple(b) for (t, start), b in self.bars.items()])
        self.latest, self.bars = {}, {}

    def prune(self, before_ms):
        with self.db:
            self.db.execute("DELETE FROM bars_1m WHERE start_ms < ?", (before_ms,))

    def close(self):
        self.flush()
        self.db.close()


def _read(db_path, sql, args=()):
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path, timeout=10)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def read_latest(db_path, tokens, now_ms, stale_ms):
    """{token: last tick + lag_ms (receipt minus exchange time), age_ms (now minus
    receipt) and stale (no tick for stale_ms)}. A token never seen is absent."""
    tokens = [str(t) for t in tokens]
    if not tokens:
        return {}
    rows = _read(db_path, "SELECT * FROM latest WHERE token IN (%s)" % ",".join("?" * len(tokens)), tokens)
    out = {}
    for token, ex, exch_ms, recv_ms, ltp, vol, oi, uc, lc in rows:
        age = now_ms - recv_ms
        out[token] = {"exchange_type": ex, "exch_ms": exch_ms, "recv_ms": recv_ms, "ltp": ltp, "cum_volume": vol,
                      "oi": oi, "upper_circuit": uc, "lower_circuit": lc, "lag_ms": recv_ms - exch_ms,
                      "age_ms": age, "stale": age > stale_ms}
    return out


def exchange_clock(db_path):
    """The newest exchange timestamp the feed has seen, in epoch ms (None before any tick)."""
    rows = _read(db_path, "SELECT max(exch_ms) FROM latest")
    return rows[0][0] if rows else None


def read_bars(db_path, token, minutes, since_ms, exchange_now_ms):
    """Closed N-minute bars resampled from the feed's 1-minute bars, aligned to
    09:15 IST: [start_ms, open, high, low, close, volume, oi, minutes with ticks].
    A bar counts only once the exchange clock is past its end; the bar in
    progress never does. Volume is the change in the day's cumulative volume
    (None when the feed has no earlier bar that day to measure from)."""
    rows = _read(db_path, "SELECT start_ms, open, high, low, close, cum_volume, oi FROM bars_1m "
                          "WHERE token = ? AND start_ms >= ? ORDER BY start_ms", (str(token), since_ms))
    span = minutes * 60000
    buckets = collections.OrderedDict()
    for start, o, h, l, c, cum, oi in rows:
        day = datetime.datetime.fromtimestamp(start / 1000, IST).replace(hour=9, minute=15, second=0, microsecond=0)
        anchor = int(day.timestamp() * 1000)
        b0 = anchor + (start - anchor) // span * span
        b = buckets.get(b0)
        if b is None:
            buckets[b0] = [b0, o, h, l, c, cum, oi, 1, anchor]
        else:
            b[2], b[3], b[4] = max(b[2], h), min(b[3], l), c
            b[5] = cum if cum is not None else b[5]
            b[6] = oi if oi is not None else b[6]
            b[7] += 1
    out, prev = [], None
    for b0, o, h, l, c, cum, oi, n, anchor in buckets.values():
        if exchange_now_ms is None or b0 + span > exchange_now_ms:
            break
        same_day = prev is not None and prev[1] == anchor
        if cum is None:
            vol = None
        elif same_day and prev[0] is not None:
            vol = cum - prev[0]
        else:
            vol = cum if b0 == anchor else None
        out.append([b0, o, h, l, c, vol, oi, n])
        prev = (cum, anchor)
    return out


# ---------------------------------------------------------------- subscriptions

def subscribe(trader, owner, items):
    """Ask the feed to stream `items` for `owner`: [{"segment": "NFO", "token": "...",
    "mode": "snapquote" | "quote" | "ltp"}]. An empty list drops the owner's tokens."""
    path = os.path.join(trader, "state", "subscriptions.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with FileLock(path + ".lock"):
        subs = read_json(path, {}) or {}
        if items:
            subs[owner] = items
        else:
            subs.pop(owner, None)
        write_json(path, subs)


def wanted(trader, cap):
    """The union of every owner's subscriptions as {(exchange code, token): mode code},
    the richer mode winning, capped at `cap` tokens. Returns (wanted, dropped count)."""
    subs = read_json(os.path.join(trader, "state", "subscriptions.json"), {}) or {}
    out = collections.OrderedDict()
    for owner in sorted(subs):
        for it in subs[owner]:
            code = SEGMENT_CODES.get(segment(it.get("segment")))
            if code and it.get("token"):
                key = (code, str(it["token"]))
                out[key] = max(out.get(key, 0), MODES.get(it.get("mode"), MODES["quote"]))
    keys = list(out)
    return collections.OrderedDict((k, out[k]) for k in keys[:cap]), max(0, len(keys) - cap)


# ---------------------------------------------------------------- capabilities

def require(trader, feature):
    """The key labels that confirmed `feature` live, or a SmartError naming the next step."""
    caps = read_json(os.path.join(trader, "state", "capabilities.json"), {}) or {}
    labels = (caps.get("confirmed") or {}).get(feature)
    if not labels:
        raise SmartError("%s is not confirmed on this account yet: run in-setup probe in market hours first" % feature)
    return labels


# ---------------------------------------------------------------- candles

INTERVALS = {"ONE_MINUTE": (1, 30), "THREE_MINUTE": (3, 60), "FIVE_MINUTE": (5, 100), "TEN_MINUTE": (10, 100),
             "FIFTEEN_MINUTE": (15, 200), "THIRTY_MINUTE": (30, 200), "ONE_HOUR": (60, 400), "ONE_DAY": (1440, 2000)}


def _spans(days, max_days):
    """Runs of consecutive dates, each at most max_days long."""
    out = []
    for d in days:
        if out and (d - out[-1][-1]).days == 1 and len(out[-1]) < max_days:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def candles(client, cache_dir, exchange, token, interval, start, end, now):
    """Closed candles for the IST dates start..end: ({"YYYY-MM-DD": [[time, o, h, l, c, v], ...]}, partial).
    Past days come from the cache and are cached forever once fetched (an empty
    day too: a holiday stays empty); today is always fetched and keeps only bars
    closed by `now`. Missing days are fetched in as few calls as the interval's
    day limit allows. The cache holds one file per month, so a year of daily
    candles is 13 reads. `partial` says what failed, or is None."""
    # ponytail: two processes filling the same month at once can drop each other's new days; they are refetched later
    minutes, max_days = INTERVALS[interval]
    folder = os.path.join(cache_dir, "candles", "%s_%s" % (exchange, token), interval)
    months = {}

    def month(day):
        key = day.strftime("%Y-%m")
        if key not in months:
            months[key] = read_json(os.path.join(folder, key + ".json"), {}) or {}
        return months[key]
    today, out, missing, d = now.date(), {}, [], start
    while d <= end:
        cached = month(d).get(d.isoformat()) if d < today else None
        if cached is None:
            missing.append(d)
        else:
            out[d.isoformat()] = cached
        d += datetime.timedelta(days=1)
    failed, touched = [], set()
    for span in _spans(missing, max_days):
        to = now.strftime("%Y-%m-%d %H:%M") if span[-1] >= today else span[-1].isoformat() + " 23:59"
        r = client.call("getCandleData", {"exchange": exchange, "symboltoken": str(token), "interval": interval,
                                          "fromdate": span[0].isoformat() + " 00:00", "todate": to})
        if not r.ok:
            failed.append("%s to %s: %s" % (span[0], span[-1], r.why().strip()))
            continue
        by_day = collections.defaultdict(list)
        for row in r.data or []:
            by_day[str(row[0])[:10]].append(row)
        for day in span:
            rows = by_day.get(day.isoformat(), [])
            if day < today:
                month(day)[day.isoformat()] = rows
                touched.add(day.strftime("%Y-%m"))
            else:
                rows = [b for b in rows if parse_ist(b[0]) + datetime.timedelta(minutes=minutes) <= now]
            out[day.isoformat()] = rows
    for key in sorted(touched):
        write_json(os.path.join(folder, key + ".json"), months[key])
    return dict(sorted(out.items())), ("; ".join(failed) or None)


# ---------------------------------------------------------------- money

PAISA = Decimal("0.01")


def tick_inr(row, scale):
    """The master's tick_size in rupees (raw 5.000000 at scale 100 is 0.05)."""
    return Decimal(str(row["tick_size"])) / Decimal(str(scale))


def round_tick(price, tick, mode="nearest"):
    """Round to a multiple of the tick: nearest (half up), down or up."""
    tick = Decimal(str(tick))
    if tick <= 0:
        raise ValueError("tick must be positive")
    rounding = {"nearest": ROUND_HALF_UP, "down": ROUND_FLOOR, "up": ROUND_CEILING}[mode]
    return ((Decimal(str(price)) / tick).quantize(Decimal(1), rounding=rounding) * tick).quantize(tick)


def max_lots_per_order(freeze_qty, lot):
    """Lots one order may carry under the exchange freeze limit."""
    # ponytail: freeze_qty read as the first quantity that freezes (NSE lists 1801 for an 1,800 limit),
    # so BSE's 1000 gives 49 lots of 20; verify both against a real ticket in Phase 4
    return max(0, (int(freeze_qty) - 1) // int(lot)) if freeze_qty and lot else None


def split_lots(lots, per_order):
    """[per_order, per_order, ..., rest]: one entry per order the ticket asks you to place.
    per_order None means no freeze limit is known; 0 means one lot already exceeds it."""
    if per_order is None:
        return [lots] if lots else []
    if per_order <= 0:
        raise SmartError("the exchange freeze limit is below one lot: this contract cannot be ordered in whole lots")
    return [per_order] * (lots // per_order) + ([lots % per_order] if lots % per_order else [])


CHARGE_KEYS = ("brokerage_per_order_inr", "stt_option_sell_pct", "exchange_txn_pct", "sebi_per_crore_inr",
               "stamp_option_buy_pct", "gst_pct")


def option_charges(side, premium, qty, segment_name, ch):
    """One executed option order's charges in rupees, from trader/charges.json.
    side BUY or SELL, segment NFO or BFO. STT is on the sell side, stamp duty on the
    buy side, GST on brokerage + exchange + SEBI. Each part rounds to the paisa."""
    missing = [k for k in CHARGE_KEYS if ch.get(k) is None]
    if missing:
        raise SmartError("charges.json is missing %s" % ", ".join(missing))
    D = lambda v: Decimal(str(v))  # noqa: E731
    turnover = D(premium) * int(qty)
    parts = collections.OrderedDict()
    parts["brokerage"] = D(ch["brokerage_per_order_inr"])
    parts["exchange"] = turnover * D(ch["exchange_txn_pct"][segment_name]) / 100
    parts["sebi"] = turnover * D(ch["sebi_per_crore_inr"]) / D(10 ** 7)
    parts["stt"] = turnover * D(ch["stt_option_sell_pct"]) / 100 if side == "SELL" else D(0)
    parts["stamp"] = turnover * D(ch["stamp_option_buy_pct"]) / 100 if side == "BUY" else D(0)
    parts["gst"] = (parts["brokerage"] + parts["exchange"] + parts["sebi"]) * D(ch["gst_pct"]) / 100
    parts = collections.OrderedDict((k, v.quantize(PAISA, ROUND_HALF_UP)) for k, v in parts.items())
    parts["total"] = sum(parts.values(), D(0))
    return parts


# ---------------------------------------------------------------- greeks

def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def year_fraction(now, expiry, close="15:30"):
    """Calendar time from `now` to the expiry date's close, in years of 365 days."""
    hh, mm = (int(x) for x in close.split(":"))
    end = datetime.datetime(expiry.year, expiry.month, expiry.day, hh, mm, tzinfo=IST)
    return max(0.0, (end - now).total_seconds()) / (365 * 86400)


def black_scholes(kind, spot, strike, years, rate, vol):
    """European option price and Greeks, no dividends. rate and vol are decimals
    (0.065, 0.14). Theta is per calendar day and vega per volatility point, the
    units the optionGreek endpoint uses."""
    if years <= 0 or vol <= 0:
        intrinsic = max(0.0, spot - strike) if kind == "CE" else max(0.0, strike - spot)
        delta = (1.0 if spot > strike else 0.0) if kind == "CE" else (-1.0 if spot < strike else 0.0)
        return {"price": intrinsic, "delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sq = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / sq
    d2 = d1 - sq
    disc = strike * math.exp(-rate * years)
    decay = -spot * _npdf(d1) * vol / (2 * math.sqrt(years))
    if kind == "CE":
        price, delta, theta = spot * _ncdf(d1) - disc * _ncdf(d2), _ncdf(d1), decay - rate * disc * _ncdf(d2)
    else:
        price, delta, theta = disc * _ncdf(-d2) - spot * _ncdf(-d1), _ncdf(d1) - 1, decay + rate * disc * _ncdf(-d2)
    return {"price": price, "delta": delta, "gamma": _npdf(d1) / (spot * sq), "theta": theta / 365,
            "vega": spot * _npdf(d1) * math.sqrt(years) / 100}


def implied_vol(kind, price, spot, strike, years, rate, lo=1e-4, hi=5.0):
    """The volatility that gives `price`, by bisection; None when no volatility in
    [lo, hi] does (a premium below intrinsic value, or no time left)."""
    if years <= 0 or price <= 0:
        return None
    f = lambda v: black_scholes(kind, spot, strike, years, rate, v)["price"] - price  # noqa: E731
    if f(lo) > 0 or f(hi) < 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    return (lo + hi) / 2


def risk_free_rate(cfg):
    """api.risk_free_rate_pct from config.json as a decimal; refuses while it is empty."""
    pct = (cfg.get("api") or {}).get("risk_free_rate_pct")
    if pct is None:
        raise SmartError("set api.risk_free_rate_pct in trader/config.json: computed Greeks need it")
    return float(pct) / 100


# ---------------------------------------------------------------- skill helpers

NOT_ADVICE = "Not financial advice: rule matches and arithmetic, never a prediction."
EXAMPLE_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "in-setup", "config.example.json")


def trader_paths(trader):
    """Every file the pack shares, by name (BRD section 7, CONTRACTS.md)."""
    state = os.path.join(trader, "state")
    j = lambda *p: os.path.join(trader, *p)  # noqa: E731
    return {"trader": trader, "state": state, "cache": j("cache"), "history": j("history"),
            "config": j("config.json"), "calendar": j("calendar.json"), "charges": j("charges.json"),
            "events": j("events.json"), "ban": j("ban.txt"), "watchlist": j("watchlist.json"),
            "news": j("news.json"), "chain_dir": j("chain"), "pick": j("pick.json"), "entry": j("entry.json"),
            "plan": j("plan.json"), "orders": j("orders.json"), "flows": j("flows.json"),
            "universe": j("universe.json"), "analyst": j("analyst.json"), "timemachine": j("timemachine.json"),
            "journal": os.path.join(state, "journal.json"), "alerted": os.path.join(state, "alerted.json"),
            "caps": os.path.join(state, "capabilities.json"), "session": os.path.join(state, "session.json"),
            "db": os.path.join(state, "ticks.sqlite"), "feed_status": os.path.join(state, "feed_status.json")}


def load_config(trader):
    """trader/config.json; a missing file is an error that names the next step."""
    cfg = read_json(trader_paths(trader)["config"])
    if cfg is None:
        raise SmartError("no %s/config.json: run in-setup init first" % trader.replace(os.sep, "/"))
    return cfg


def require_account(cfg):
    """Capital in rupees; the trading skills refuse to run while it is empty."""
    capital = (cfg.get("account") or {}).get("capital_inr")
    if not capital:
        raise SmartError("set account.capital_inr in trader/config.json: the pack refuses to size or check "
                         "trades without it")
    return Decimal(str(capital))


def stamp(now):
    return "%s %s" % (WEEKDAYS[now.weekday()], now.strftime("%Y-%m-%d %H:%M"))


def open_client(trader, now, offline=None, record=None):
    """(client, limiter) ready for secure calls. Offline: replies replayed from
    DIR/<endpoint>.json, a pinned clock, no limiter, no login, no network. Live:
    credentials from the environment or ./.env, the shared limiter, and today's
    session (reused, refreshed or a new login)."""
    if offline:
        epoch = now.timestamp()
        c = Client("OFFLINE", transport=Replay(offline), clock=lambda: epoch, sleep=lambda s: None)
        c.jwt = "OFFLINE"
        return c, None
    p = trader_paths(trader)
    cfg = read_json(p["config"]) or {}
    creds = credentials()
    api = cfg.get("api") or {}
    ips = {"local": local_ip(), "mac": mac_address(),
           "public": public_ip(api.get("client_public_ip"), p["state"], now.date().isoformat())}
    limiter = Limiter(p["state"], caps_from_config(cfg.get("limits")))
    c = Client(creds["api_key"], limiter=limiter, record=record, ips=ips)
    Session(c, creds, p["session"], now.date().isoformat()).start()
    return c, limiter


def usage_text(limiter):
    return limiter.usage_line() if limiter else "API usage: none (offline replay)"


INTERVAL_NAMES = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE", 10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE",
                  30: "THIRTY_MINUTE", 60: "ONE_HOUR"}


def day_bars(trader, exchange, token, minutes, now, client=None):
    """Today's closed N-minute bars for one token, from the live feed when it has
    today's data from 09:15, else from getCandleData through the candle cache (a
    feed started late cannot rebuild the opening range; without a client its
    bars are returned marked partial).
    Returns {"bars": [{"start_ist": "09:15", "start_ms", "open", "high", "low",
    "close", "volume"}], "source": "feed" | "candles" | None, "partial": None or why}."""
    p = trader_paths(trader)
    anchor = int(now.replace(hour=9, minute=15, second=0, microsecond=0).timestamp() * 1000)
    now_ms = int(now.timestamp() * 1000)
    clock = exchange_clock(p["db"])
    if clock and clock >= anchor:
        rows = read_bars(p["db"], token, minutes, anchor, min(clock, now_ms))
        late = bool(rows) and rows[0][0] > anchor
        late_at = datetime.datetime.fromtimestamp(rows[0][0] / 1000, IST).strftime("%H:%M") if late else None
        if rows and not (late and client is not None):
            return {"bars": [{"start_ist": datetime.datetime.fromtimestamp(b[0] / 1000, IST).strftime("%H:%M"),
                              "start_ms": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4],
                              "volume": b[5]} for b in rows], "source": "feed",
                    "partial": "the feed started after 09:15" if late else None}
    if client is None:
        return {"bars": [], "source": None, "partial": "no live feed data and no API client for candles"}
    days, partial = candles(client, p["cache"], exchange, token, INTERVAL_NAMES[minutes], now.date(), now.date(), now)
    rows = days.get(now.date().isoformat(), [])
    out = {"bars": [{"start_ist": str(b[0])[11:16], "start_ms": int(parse_ist(b[0]).timestamp() * 1000),
                     "open": b[1], "high": b[2], "low": b[3], "close": b[4], "volume": b[5]} for b in rows],
           "source": "candles", "partial": partial}
    if clock and clock >= anchor and late_at:
        out["late_feed"] = late_at   # the feed's first bar today, after 09:15
    return out


def opening_range(bars, start="09:15", end="09:30", minutes=5):
    """High and low of the closed bars from start to end (09:15 to 09:30: three
    5-minute bars). None until every one of those bars has closed."""
    def mins(hm):
        h, m = hm.split(":")
        return int(h) * 60 + int(m)
    need = (mins(end) - mins(start)) // minutes
    inside = [b for b in bars if mins(start) <= mins(b["start_ist"]) < mins(end)]
    if len(inside) < need:
        return None
    return {"high": max(b["high"] for b in inside), "low": min(b["low"] for b in inside),
            "start": start, "end": end, "bars": len(inside)}

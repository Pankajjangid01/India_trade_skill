"""Synthetic NIFTY 5-minute candles for the in-timemachine tests. Not real data.

A seeded random walk, every weekday from 2026-08-03 to 2026-11-23 except
2026-10-02 (a holiday the test calendar does not list), 75 bars a day from 09:15,
shaped like getCandleData's documented reply: [time, open, high, low, close,
volume], volume 0 as for an index. Plain arithmetic on random.Random(11), so the
same bars on every machine.

    python tests/fixtures/in-timemachine/candles.py DIR   # writes DIR/getCandleData.json (and master.json)
"""

import datetime
import json
import os
import random
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIRST, LAST, HOLIDAY = datetime.date(2026, 8, 3), datetime.date(2026, 11, 23), "2026-10-02"


def to_tick(x):
    return round(x * 20) / 20


def rows(seed=11):
    rng = random.Random(seed)

    def noise(scale):   # roughly normal, from three uniforms
        return (rng.random() + rng.random() + rng.random() - 1.5) * scale
    price, out, d = 22500.0, [], FIRST
    while d <= LAST:
        if d.weekday() < 5 and d.isoformat() != HOLIDAY:
            drift = noise(0.0001)
            price *= 1 + noise(0.006)
            for i in range(75):
                o = price
                c = o * (1 + drift + noise(0.006 if i < 3 else 0.0016))   # a wider opening range
                h = max(o, c) * (1 + abs(noise(0.0008)))
                low = min(o, c) * (1 - abs(noise(0.0008)))
                t = datetime.datetime(d.year, d.month, d.day, 9, 15) + datetime.timedelta(minutes=5 * i)
                out.append([t.strftime("%Y-%m-%dT%H:%M:00+05:30"), to_tick(o), to_tick(h), to_tick(low), to_tick(c), 0])
                price = c
        d += datetime.timedelta(days=1)
    return out


def reply(data):
    return {"status": 200, "headers": {}, "body": {"status": True, "message": "SUCCESS", "errorcode": "", "data": data}}


def write(folder, data=None):
    """DIR/getCandleData.json (one reply with every bar) and DIR/master.json: an --offline folder."""
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "getCandleData.json"), "w", encoding="utf-8") as fh:
        json.dump(reply(rows() if data is None else data), fh)
    shutil.copy(os.path.join(HERE, "master.json"), os.path.join(folder, "master.json"))
    return folder


if __name__ == "__main__":
    write(sys.argv[1])

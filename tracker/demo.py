"""Synthetic data so the dashboard can be explored before real history accumulates.

Writes to a separate database (data/demo.db by default) with obviously fake
game names, so it can never be mistaken for collected data.
"""
from __future__ import annotations

import json
import math
import random
from datetime import timedelta

from sqlalchemy.engine import Engine

from . import db

DEMO_GAMES = [
    # appid, name, base players, price cents
    (9_000_001, "[Demo] Starfall Tactics", 18_000, 2999),
    (9_000_002, "[Demo] Hollow Orchard", 6_500, 1499),
    (9_000_003, "[Demo] Neon Drift Racing", 42_000, 3999),
    (9_000_004, "[Demo] Pocket Dungeon", 2_200, 999),
]


def generate(engine: Engine, days: int = 90, seed: int = 7) -> None:
    rng = random.Random(seed)
    now = db.utcnow().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)

    db.upsert(engine, db.games, [{
        "appid": a, "name": n, "is_free": False, "developers": "Demo Studio", "publishers": "Demo Publishing",
        "release_date": "Jan 1, 2024", "header_image": "", "updated_at": now,
    } for a, n, _, _ in DEMO_GAMES], ["appid"])

    # One shared "seasonal" sale for every game, so the market-wide sale detection has something to find.
    seasonal = (start + timedelta(days=days - 26, hours=17), start + timedelta(days=days - 16, hours=17), 40)

    for appid, name, base, price in DEMO_GAMES:
        # Two sales and two patches per game, at random points, plus the seasonal sale.
        sales = [seasonal]
        for _ in range(2):
            s = start + timedelta(days=rng.randint(10, days - 12), hours=17)
            sales.append((s, s + timedelta(days=rng.choice([7, 10, 14])), rng.choice([25, 33, 50, 60, 75])))
        patches = [start + timedelta(days=rng.randint(5, days - 3), hours=rng.randint(14, 20)) for _ in range(2)]
        sale_lift = rng.uniform(0.25, 0.7)
        patch_lift = rng.uniform(0.15, 0.45)

        players, prices, events = [], [], []
        prev_disc = 0
        ts = start
        trend = 1.0
        while ts <= now:
            trend *= 1 + rng.gauss(-0.00005, 0.002)
            hour = ts.hour
            daily = 0.65 + 0.35 * math.sin((hour - 13) / 24 * 2 * math.pi)  # peaks ~19:00 UTC
            weekly = 1.12 if ts.weekday() >= 5 else 1.0
            mult = 1.0
            disc = 0
            for s, e, pct in sales:
                if s <= ts < e:
                    disc = pct
                    ramp = min(1.0, (ts - s).total_seconds() / 86400 / 1.5)
                    mult += sale_lift * (pct / 50) * ramp
                elif e <= ts < e + timedelta(days=10):
                    mult += sale_lift * 0.4 * math.exp(-(ts - e).total_seconds() / 86400 / 3)  # new-owner tail
            for p in patches:
                if ts >= p:
                    mult += patch_lift * math.exp(-(ts - p).total_seconds() / 86400 / 4)
            n = base * trend * daily * weekly * mult * rng.uniform(0.95, 1.05)
            players.append({"appid": appid, "ts": ts, "players": max(0, int(n))})

            final = round(price * (100 - disc) / 100)
            prices.append({"appid": appid, "ts": ts, "country": "us", "currency": "USD",
                           "initial_cents": price, "final_cents": final, "discount_pct": disc, "is_free": False})
            if disc != prev_disc:
                kind = "sale_start" if prev_disc == 0 else ("sale_end" if disc == 0 else "sale_change")
                title = f"{disc}% off - {final / 100:.2f} USD" if disc else f"Sale ended - back to {price / 100:.2f} USD"
                events.append({"appid": appid, "ts": ts, "kind": kind, "title": title, "url": None,
                               "detail": json.dumps({"discount_pct": disc, "previous_discount_pct": prev_disc}),
                               "source_id": f"us:{ts.isoformat()}"})
                prev_disc = disc
            ts += timedelta(hours=1)

        for i, p in enumerate(sorted(patches), 1):
            events.append({"appid": appid, "ts": p, "kind": "patch", "title": f"Patch 1.{i}.0 - balance and content update",
                           "url": None, "detail": "{}", "source_id": f"demo-patch-{appid}-{i}"})

        owners = []
        low = rng.choice([500_000, 1_000_000, 2_000_000])
        neg_ratio = rng.uniform(0.06, 0.15)
        for d in range(days + 1):
            day = (start + timedelta(days=d)).date()
            if d and d % 30 == 0 and rng.random() < 0.5:
                low *= 2
            pos = int(base * 3 + d * base * 0.02)
            owners.append({"appid": appid, "date": day, "owners_low": low, "owners_high": low * 2,
                           "ccu": int(base * 1.1), "positive": pos, "negative": int(pos * (neg_ratio + d * rng.uniform(-0.0002, 0.0004))),
                           "avg_playtime_min": 1200 + d * 3, "median_playtime_min": 600 + d})

        # Steam review totals: new reviews per day roughly track the day's peak players.
        daily_peak: dict = {}
        for r in players:
            daily_peak[r["ts"].date()] = max(daily_peak.get(r["ts"].date(), 0), r["players"])
        total = base * 40
        reviews = []
        for day in sorted(daily_peak):
            total += int(daily_peak[day] * rng.uniform(0.003, 0.006))
            reviews.append({"appid": appid, "date": day, "total_reviews": total})

        db.insert_ignore(engine, db.player_snapshots, players)
        db.insert_ignore(engine, db.price_snapshots, prices)
        db.insert_ignore(engine, db.events, events)
        db.insert_ignore(engine, db.owner_snapshots, owners)
        db.insert_ignore(engine, db.review_snapshots, reviews)

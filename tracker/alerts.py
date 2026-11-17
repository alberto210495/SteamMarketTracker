"""Discord alerts, sent at the end of each collection run.

- sales / patches: events created since the last alert (cursor kept in app_state)
- spikes: latest players vs. the median for the same hour of day over 14 days
- daily summary: once per UTC day, on the first run at or after the configured hour
- review bombs: the latest day's new negative reviews far above the usual level

No pandas here: this runs in the scheduled job with requirements-worker.txt only.
A failed Discord send never advances the bookkeeping, so nothing is lost.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

import requests
from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from . import db
from .config import AlertsConfig

log = logging.getLogger(__name__)

EVENT_MAX_AGE = timedelta(hours=48)   # don't announce old news (e.g. history of a newly added game)
SPIKE_LOOKBACK = timedelta(days=14)
SPIKE_MIN_SAMPLES = 5
SPIKE_COOLDOWN = timedelta(hours=24)

SALE_KINDS = ("sale_start", "sale_end", "sale_change", "price_change")
PATCH_KINDS = ("patch",)

COLORS = {  # Discord embed sidebar colours
    "sale_start": 0x1BAF7A, "sale_change": 0x1BAF7A, "sale_end": 0x898781, "price_change": 0xEDA100,
    "patch": 0x2A78D6, "spike": 0xEB6834, "summary": 0x6250D6, "test": 0x2A78D6, "bomb": 0xE34948,
}
LABELS = {"sale_start": "On sale", "sale_end": "Sale ended", "sale_change": "Discount changed",
          "price_change": "Price change", "patch": "New patch"}

Sender = Callable[[list[dict[str, Any]]], bool]


def store_url(appid: int) -> str:
    return f"https://store.steampowered.com/app/{appid}/"


def discord_sender(webhook_url: str, username: str = "Steam Tracker") -> Sender:
    """Returns a function that posts embeds (max 10 per message) and reports success."""
    session = requests.Session()

    def send(embeds: list[dict[str, Any]]) -> bool:
        for i in range(0, len(embeds), 10):
            payload = {"username": username, "embeds": embeds[i:i + 10], "allowed_mentions": {"parse": []}}
            for attempt in range(3):
                try:
                    resp = session.post(webhook_url, json=payload, timeout=15)
                except requests.RequestException as exc:
                    log.warning("Discord post failed: %s", type(exc).__name__)
                    time.sleep(2)
                    continue
                if resp.status_code in (200, 204):
                    break
                if resp.status_code == 429:
                    time.sleep(float(resp.json().get("retry_after", 2)))
                    continue
                log.warning("Discord returned HTTP %d", resp.status_code)  # never log the URL
                return False
            else:
                return False
        return True

    return send


@dataclass
class AlertStats:
    events: int = 0
    spikes: int = 0
    bombs: int = 0
    summary: bool = False
    failures: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers

def _names(engine: Engine) -> dict[int, str]:
    return {a: (r.get("name") or f"App {a}") for a, r in db.game_rows(engine).items()}


def _fmt_int(n: float | int | None) -> str:
    return "?" if n is None else f"{int(round(n)):,}"


# --------------------------------------------------------------------------- events (sales, patches)

def event_embeds(engine: Engine, cfg: AlertsConfig, now: datetime) -> tuple[list[dict], int | None]:
    """Embeds for new events, and the cursor value to store once they're sent.
    First run ever: returns no embeds and just initialises the cursor."""
    t = db.events
    with engine.connect() as conn:
        max_id = conn.execute(select(func.max(t.c.id))).scalar()
    if max_id is None:
        return [], None
    cursor = db.get_state(engine, "alerts.events_cursor")
    if cursor is None:
        return [], max_id  # don't flood Discord with everything collected so far

    kinds = (SALE_KINDS if cfg.sales else ()) + (PATCH_KINDS if cfg.patches else ())
    if not kinds:
        return [], max_id
    q = (select(t).where(t.c.id > int(cursor), t.c.kind.in_(kinds), t.c.ts >= now - EVENT_MAX_AGE)
         .order_by(t.c.ts, t.c.id))
    with engine.connect() as conn:
        rows = conn.execute(q).mappings().all()
    names = _names(engine)
    embeds = []
    for ev in rows:
        embed = {
            "title": f"{LABELS.get(ev['kind'], ev['kind'])}: {names.get(ev['appid'], ev['appid'])}",
            "description": (ev["title"] or "")[:300],
            "url": ev["url"] or store_url(ev["appid"]),
            "color": COLORS.get(ev["kind"], 0x898781),
            "timestamp": ev["ts"].isoformat() + "Z",
        }
        embeds.append(embed)
    return embeds, max_id


# --------------------------------------------------------------------------- spikes

def detect_spike(latest: int, same_hour_history: list[int], cfg: AlertsConfig) -> float | None:
    """Returns the usual (median) level if `latest` counts as a spike, else None."""
    if len(same_hour_history) < SPIKE_MIN_SAMPLES:
        return None
    usual = statistics.median(same_hour_history)
    if usual <= 0:
        return None
    if latest >= usual * cfg.spike_ratio and latest - usual >= cfg.spike_min_increase:
        return usual
    return None


def spike_embeds(engine: Engine, cfg: AlertsConfig, appids: list[int], now: datetime) -> tuple[list[dict], list[str]]:
    t = db.player_snapshots
    names = _names(engine)
    embeds, keys = [], []
    for appid in appids:
        with engine.connect() as conn:
            latest = conn.execute(select(t.c.ts, t.c.players).where(t.c.appid == appid)
                                  .order_by(t.c.ts.desc()).limit(1)).first()
            if not latest or latest.ts < now - timedelta(hours=2):
                continue
            hist = conn.execute(select(t.c.ts, t.c.players).where(
                t.c.appid == appid, t.c.ts >= latest.ts - SPIKE_LOOKBACK, t.c.ts < latest.ts - timedelta(hours=12),
            )).all()
        same_hour = [r.players for r in hist if r.ts.hour == latest.ts.hour]
        usual = detect_spike(latest.players, same_hour, cfg)
        if usual is None:
            continue
        key = f"alerts.spike.{appid}"
        last = db.get_state(engine, key)
        if last and now - datetime.fromisoformat(last) < SPIKE_COOLDOWN:
            continue
        embeds.append({
            "title": f"Player spike: {names.get(appid, appid)}",
            "description": (f"**{_fmt_int(latest.players)}** players right now, "
                            f"{latest.players / usual:.1f}× the usual {_fmt_int(usual)} at this hour."),
            "url": store_url(appid),
            "color": COLORS["spike"],
            "timestamp": latest.ts.isoformat() + "Z",
        })
        keys.append(key)
    return embeds, keys


# --------------------------------------------------------------------------- review bombs

BOMB_LOOKBACK_DAYS = 14
BOMB_RATIO = 3.0


def is_review_bomb(new_negative: float, new_positive: float, history_negative: list[float],
                   min_negative: int = 30) -> bool:
    """A day counts as a review bomb when negative reviews are at least `min_negative`,
    at least 3x the median of the previous days, and the majority of that day's reviews."""
    if new_negative < min_negative or new_negative <= new_positive:
        return False
    usual = statistics.median(history_negative) if history_negative else 0
    return new_negative >= BOMB_RATIO * max(usual, 1)


def daily_review_split(rows: list[Any]) -> list[tuple[Any, float, float]]:
    """[(date, total_positive, total_negative)] sorted -> [(date, new_pos/day, new_neg/day)]."""
    out = []
    for prev, cur in zip(rows, rows[1:]):
        days = max((cur[0] - prev[0]).days, 1)
        out.append((cur[0], max(cur[1] - prev[1], 0) / days, max(cur[2] - prev[2], 0) / days))
    return out


def bomb_embeds(engine: Engine, cfg: AlertsConfig, appids: list[int], now: datetime) -> tuple[list[dict], list[tuple]]:
    t = db.review_score_snapshots
    names = _names(engine)
    embeds, marks = [], []
    for appid in appids:
        with engine.connect() as conn:
            rows = conn.execute(select(t.c.date, t.c.total_positive, t.c.total_negative)
                                .where(t.c.appid == appid).order_by(t.c.date.desc())
                                .limit(BOMB_LOOKBACK_DAYS + 2)).all()
        split = daily_review_split(sorted((r.date, r.total_positive, r.total_negative) for r in rows))
        if len(split) < 4:
            continue
        day, pos, neg = split[-1]
        if (now.date() - day).days > 1:
            continue
        if not is_review_bomb(neg, pos, [n for _, _, n in split[:-1]], cfg.review_bomb_min_negative):
            continue
        key = f"alerts.bomb.{appid}"
        if db.get_state(engine, key) == day.isoformat():
            continue
        share = neg / (neg + pos) * 100 if neg + pos else 100
        embeds.append({
            "title": f"Review bomb: {names.get(appid, appid)}",
            "description": (f"**{_fmt_int(neg)}** negative reviews on {day:%b %d} ({share:.0f}% of the day's reviews), "
                            f"vs. a usual {_fmt_int(statistics.median([n for _, _, n in split[:-1]]))} per day."),
            "url": f"https://store.steampowered.com/app/{appid}/#app_reviews_hash",
            "color": COLORS["bomb"],
            "timestamp": now.isoformat() + "Z",
        })
        marks.append((key, day.isoformat()))
    return embeds, marks


# --------------------------------------------------------------------------- daily summary

def summary_embed(engine: Engine, appids: list[int], now: datetime, country: str | None = None) -> dict | None:
    t, p = db.player_snapshots, db.price_snapshots
    names = _names(engine)
    lines = []
    for appid in appids:
        with engine.connect() as conn:
            peak = conn.execute(select(func.max(t.c.players)).where(
                t.c.appid == appid, t.c.ts > now - timedelta(hours=24))).scalar()
            prev = conn.execute(select(func.max(t.c.players)).where(
                t.c.appid == appid, t.c.ts > now - timedelta(hours=48), t.c.ts <= now - timedelta(hours=24))).scalar()
            pq = select(p).where(p.c.appid == appid)
            if country:
                pq = pq.where(p.c.country == country)
            price = conn.execute(pq.order_by(p.c.ts.desc()).limit(1)).mappings().first()
        if peak is None:
            continue
        change = f" ({(peak - prev) / prev * 100:+.0f}%)" if prev else ""
        if price is None:
            price_txt = ""
        elif price["is_free"]:
            price_txt = " · free"
        else:
            price_txt = f" · {price['final_cents'] / 100:.2f} {price['currency'] or ''}".rstrip()
            if price["discount_pct"]:
                price_txt += f" (-{price['discount_pct']}%)"
        lines.append((peak, f"**{names.get(appid, appid)}**: {_fmt_int(peak)} peak{change}{price_txt}"))
    if not lines:
        return None
    lines.sort(key=lambda x: -x[0])
    return {
        "title": f"Daily summary · {now:%b %d}",
        "description": "\n".join(line for _, line in lines)[:4000] + "\n\n24h peak players, change vs. the day before.",
        "color": COLORS["summary"],
        "timestamp": now.isoformat() + "Z",
    }


# --------------------------------------------------------------------------- entry point

def process(engine: Engine, cfg: AlertsConfig, appids: list[int], send: Sender, now: datetime | None = None,
            country: str | None = None) -> AlertStats:
    now = now or db.utcnow()
    stats = AlertStats()

    try:
        embeds, cursor = event_embeds(engine, cfg, now)
        if not embeds or send(embeds):
            if cursor is not None:
                db.set_state(engine, "alerts.events_cursor", str(cursor))
            stats.events = len(embeds)
        else:
            stats.failures.append("events")
    except Exception:
        log.exception("Event alerts failed")
        stats.failures.append("events")

    if cfg.spikes:
        try:
            embeds, keys = spike_embeds(engine, cfg, appids, now)
            if embeds and send(embeds):
                for k in keys:
                    db.set_state(engine, k, now.isoformat())
                stats.spikes = len(embeds)
            elif embeds:
                stats.failures.append("spikes")
        except Exception:
            log.exception("Spike alerts failed")
            stats.failures.append("spikes")

    if cfg.review_bombs:
        try:
            embeds, marks = bomb_embeds(engine, cfg, appids, now)
            if embeds and send(embeds):
                for k, v in marks:
                    db.set_state(engine, k, v)
                stats.bombs = len(embeds)
            elif embeds:
                stats.failures.append("bombs")
        except Exception:
            log.exception("Review bomb alerts failed")
            stats.failures.append("bombs")

    if cfg.daily_summary and now.hour >= cfg.daily_summary_hour_utc:
        key, today = "alerts.daily_summary", now.date().isoformat()
        try:
            if db.get_state(engine, key) != today:
                embed = summary_embed(engine, appids, now, country)
                if embed and send([embed]):
                    db.set_state(engine, key, today)
                    stats.summary = True
                elif embed:
                    stats.failures.append("summary")
        except Exception:
            log.exception("Daily summary failed")
            stats.failures.append("summary")

    log.info("Alerts: events=%d spikes=%d bombs=%d summary=%s%s", stats.events, stats.spikes, stats.bombs,
             stats.summary,
             f" failures={stats.failures}" if stats.failures else "")
    return stats


def send_test(send: Sender) -> bool:
    return send([{
        "title": "Steam Tracker is connected",
        "description": "Alerts for sales, patches, player spikes, review bombs and a daily summary will appear "
                       "in this channel.",
        "color": COLORS["test"],
        "timestamp": db.utcnow().isoformat() + "Z",
    }])

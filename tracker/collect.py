"""One collection pass. Designed to be run hourly by cron / GitHub Actions.

Idempotent: re-running within the same hour/day does not duplicate data.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.engine import Engine

from . import config, db
from .steam import (PriceInfo, SteamClient, classify_news, parse_owner_range, parse_review_total,
                    unix_to_utc)

log = logging.getLogger(__name__)

METADATA_MAX_AGE = timedelta(days=7)
ALL_STEPS = ("metadata", "players", "prices", "news", "owners", "reviews", "sentiment",
             "alerts")  # alerts run last


@dataclass
class RunStats:
    players: int = 0
    prices: int = 0
    events: int = 0
    news: int = 0
    owners: int = 0
    reviews: int = 0
    sentiment: int = 0
    metadata: int = 0
    failures: list[str] = field(default_factory=list)

    def summary(self) -> str:
        s = (f"players={self.players} prices={self.prices} price_events={self.events} "
             f"news={self.news} owners={self.owners} reviews={self.reviews} sentiment={self.sentiment} "
             f"metadata={self.metadata}")
        if self.failures:
            s += f" failures={len(self.failures)}"
        return s


def fmt_money(cents: int | None, currency: str | None) -> str:
    if cents is None:
        return "?"
    return f"{cents / 100:.2f} {currency or ''}".strip()


def detect_price_events(prev: dict[str, Any] | None, new: PriceInfo) -> list[tuple[str, str, dict]]:
    """Compare the previous stored snapshot to a fresh price. Returns (kind, title, detail) tuples.

    The first snapshot ever only produces an event if the game is already on sale,
    so a sale in progress when tracking starts still gets shaded on charts.
    """
    out: list[tuple[str, str, dict]] = []
    new_d = new.discount_pct or 0
    if prev is None:
        if new_d > 0:
            out.append(("sale_start", f"{new_d}% off (already on sale when tracking began)",
                        {"discount_pct": new_d, "final_cents": new.final_cents, "currency": new.currency}))
        return out

    old_d = prev.get("discount_pct") or 0
    if old_d == 0 and new_d > 0:
        out.append(("sale_start", f"{new_d}% off - {fmt_money(new.final_cents, new.currency)}",
                    {"discount_pct": new_d, "final_cents": new.final_cents, "currency": new.currency}))
    elif old_d > 0 and new_d == 0:
        out.append(("sale_end", f"Sale ended - back to {fmt_money(new.final_cents, new.currency)}",
                    {"previous_discount_pct": old_d, "final_cents": new.final_cents, "currency": new.currency}))
    elif old_d > 0 and new_d > 0 and old_d != new_d:
        out.append(("sale_change", f"Discount {old_d}% -> {new_d}%",
                    {"previous_discount_pct": old_d, "discount_pct": new_d, "final_cents": new.final_cents}))

    old_base = prev.get("initial_cents")
    if (old_base is not None and new.initial_cents is not None and old_base != new.initial_cents
            and prev.get("currency") == new.currency and not (prev.get("is_free") or new.is_free)):
        out.append(("price_change",
                    f"Base price {fmt_money(old_base, new.currency)} -> {fmt_money(new.initial_cents, new.currency)}",
                    {"previous_initial_cents": old_base, "initial_cents": new.initial_cents}))
    if bool(prev.get("is_free")) != new.is_free:
        out.append(("price_change", "Went free-to-play" if new.is_free else "No longer free",
                    {"is_free": new.is_free}))
    return out


# --------------------------------------------------------------------------- steps

def sync_metadata(engine: Engine, client: SteamClient, appids: list[int], country: str, stats: RunStats) -> None:
    existing = db.game_rows(engine)
    now = db.utcnow()
    for appid in appids:
        row = existing.get(appid)
        if row and row.get("name") and row.get("updated_at") and now - row["updated_at"] < METADATA_MAX_AGE:
            continue
        details = client.app_details(appid, country)
        if not details:
            stats.failures.append(f"metadata:{appid}")
            if not row:  # keep a placeholder so the game still shows up
                db.upsert(engine, db.games, [{"appid": appid, "name": f"App {appid}", "updated_at": None}], ["appid"])
            continue
        db.upsert(engine, db.games, [{
            "appid": appid,
            "name": details.get("name") or f"App {appid}",
            "is_free": bool(details.get("is_free")),
            "developers": ", ".join(details.get("developers") or [])[:512],
            "publishers": ", ".join(details.get("publishers") or [])[:512],
            "release_date": ((details.get("release_date") or {}).get("date") or "")[:64],
            "header_image": (details.get("header_image") or "")[:512],
            "updated_at": now,
        }], ["appid"])
        stats.metadata += 1


def collect_players(engine: Engine, client: SteamClient, appids: list[int], ts: datetime, stats: RunStats) -> None:
    rows = []
    for appid in appids:
        n = client.current_players(appid)
        if n is None:
            stats.failures.append(f"players:{appid}")
            continue
        rows.append({"appid": appid, "ts": ts, "players": n})
    stats.players += db.insert_ignore(engine, db.player_snapshots, rows)


def collect_prices(engine: Engine, client: SteamClient, appids: list[int], country: str,
                   ts: datetime, stats: RunStats) -> None:
    prices = client.prices(appids, country)
    for appid in appids:
        info = prices.get(appid)
        if info is None:
            stats.failures.append(f"prices:{appid}")
            continue
        prev = db.latest_price(engine, appid, country)
        if prev and prev["ts"] >= ts:
            continue  # already sampled this hour
        event_rows = [{
            "appid": appid, "ts": ts, "kind": kind, "title": title, "url": None,
            "detail": json.dumps(detail), "source_id": f"{country}:{ts.isoformat()}",
        } for kind, title, detail in detect_price_events(prev, info)]
        db.insert_ignore(engine, db.price_snapshots, [{
            "appid": appid, "ts": ts, "country": country, "currency": info.currency,
            "initial_cents": info.initial_cents, "final_cents": info.final_cents,
            "discount_pct": info.discount_pct, "is_free": info.is_free,
        }])
        db.insert_ignore(engine, db.events, event_rows)
        stats.prices += 1
        stats.events += len(event_rows)


def collect_news(engine: Engine, client: SteamClient, appids: list[int], stats: RunStats) -> None:
    for appid in appids:
        items = client.news(appid)
        rows = []
        for it in items:
            if not it.get("gid") or not it.get("date"):
                continue
            rows.append({
                "appid": appid,
                "ts": unix_to_utc(it["date"]),
                "kind": classify_news(it),
                "title": (it.get("title") or "")[:512],
                "url": (it.get("url") or "")[:1024],
                "detail": json.dumps({"feed": it.get("feedname"), "tags": it.get("tags") or []}),
                "source_id": str(it["gid"]),
            })
        stats.news += db.insert_ignore(engine, db.events, rows)


def collect_owners(engine: Engine, client: SteamClient, appids: list[int], ts: datetime, stats: RunStats) -> None:
    """SteamSpy refreshes about once a day, so only fetch once per UTC day."""
    day = ts.date()
    for appid in appids:
        if db.has_owner_snapshot(engine, appid, day):
            continue
        data = client.steamspy(appid)
        if not data:
            stats.failures.append(f"owners:{appid}")
            continue
        low, high = parse_owner_range(data.get("owners"))
        db.insert_ignore(engine, db.owner_snapshots, [{
            "appid": appid, "date": day, "owners_low": low, "owners_high": high,
            "ccu": data.get("ccu"), "positive": data.get("positive"), "negative": data.get("negative"),
            "avg_playtime_min": data.get("average_forever"), "median_playtime_min": data.get("median_forever"),
        }])
        stats.owners += 1


def collect_reviews(engine: Engine, client: SteamClient, appids: list[int], country: str,
                    ts: datetime, stats: RunStats) -> None:
    """Steam's total review count, once per UTC day (new reviews/day = daily difference)."""
    day = ts.date()
    for appid in appids:
        if db.has_review_snapshot(engine, appid, day):
            continue
        total = parse_review_total(client.app_details(appid, country))
        if total is None:
            stats.failures.append(f"reviews:{appid}")
            continue
        db.insert_ignore(engine, db.review_snapshots, [{"appid": appid, "date": day, "total_reviews": total}])
        stats.reviews += 1


def collect_sentiment(engine: Engine, client: SteamClient, appids: list[int], ts: datetime,
                      stats: RunStats) -> None:
    """Steam's positive/negative review totals, once per UTC day."""
    day = ts.date()
    for appid in appids:
        if db.has_snapshot(engine, db.review_score_snapshots, appid, day):
            continue
        summary = client.review_summary(appid)
        if summary is None:
            stats.failures.append(f"sentiment:{appid}")
            continue
        db.insert_ignore(engine, db.review_score_snapshots, [{"appid": appid, "date": day, **summary}])
        stats.sentiment += 1


def run(steps: tuple[str, ...] = ALL_STEPS, engine: Engine | None = None,
        client: SteamClient | None = None, games: config.GamesConfig | None = None,
        send_alerts=None) -> RunStats:
    """`send_alerts` overrides the Discord sender (tests); by default alerts go to
    DISCORD_WEBHOOK_URL when it's set and are skipped otherwise."""
    engine = engine or db.get_engine()
    client = client or SteamClient(config.steam_api_key())
    games = games or config.load_games()
    stats = RunStats()
    # Truncate to the hour so a sample is keyed by "which hour", and re-runs are no-ops.
    ts = db.utcnow().replace(minute=0, second=0, microsecond=0)
    # games.toml plus games added in the dashboard, minus games removed there
    appids = db.tracked_appids(engine, games.appids)
    log.info("Collecting %s for %d games at %s UTC", ",".join(steps), len(appids), ts.isoformat())

    step_fns = {
        "metadata": lambda: sync_metadata(engine, client, appids, games.country, stats),
        "players": lambda: collect_players(engine, client, appids, ts, stats),
        "prices": lambda: collect_prices(engine, client, appids, games.country, ts, stats),
        "news": lambda: collect_news(engine, client, appids, stats),
        "owners": lambda: collect_owners(engine, client, appids, ts, stats),
        "reviews": lambda: collect_reviews(engine, client, appids, games.country, ts, stats),
        "sentiment": lambda: collect_sentiment(engine, client, appids, ts, stats),
    }
    for step in ALL_STEPS:
        if step not in steps or step not in step_fns:
            continue
        try:
            step_fns[step]()
        except Exception:  # one broken step should not lose the others
            log.exception("Step %s failed", step)
            stats.failures.append(f"step:{step}")
    log.info("Done: %s", stats.summary())
    if stats.failures:
        log.warning("Failures: %s", ", ".join(stats.failures))

    if "alerts" in steps:
        webhook = config.discord_webhook_url()
        if send_alerts is None and webhook:
            from .alerts import discord_sender
            send_alerts = discord_sender(webhook)
        if send_alerts is not None:
            from . import alerts
            try:  # alerts must never fail the collection run
                alerts.process(engine, games.alerts, appids, send_alerts, now=db.utcnow())
            except Exception:
                log.exception("Alerts failed")
    return stats

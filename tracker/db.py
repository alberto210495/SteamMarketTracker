"""Schema and storage helpers (SQLAlchemy Core; works on SQLite and PostgreSQL).

All timestamps are stored as naive UTC datetimes.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine

from . import config

metadata = MetaData()

games = Table(
    "games",
    metadata,
    Column("appid", Integer, primary_key=True),
    Column("name", String(255)),
    Column("is_free", Boolean),
    Column("developers", String(512)),
    Column("publishers", String(512)),
    Column("release_date", String(64)),
    Column("header_image", String(512)),
    Column("updated_at", DateTime),
)

# Hourly concurrent-player samples (ISteamUserStats/GetNumberOfCurrentPlayers)
player_snapshots = Table(
    "player_snapshots",
    metadata,
    Column("appid", Integer, primary_key=True),
    Column("ts", DateTime, primary_key=True),
    Column("players", Integer, nullable=False),
)

# Storefront price samples (store.steampowered.com/api/appdetails)
price_snapshots = Table(
    "price_snapshots",
    metadata,
    Column("appid", Integer, primary_key=True),
    Column("ts", DateTime, primary_key=True),
    Column("country", String(4), primary_key=True),
    Column("currency", String(8)),
    Column("initial_cents", Integer),
    Column("final_cents", Integer),
    Column("discount_pct", Integer, nullable=False, default=0),
    Column("is_free", Boolean, nullable=False, default=False),
)

# Daily SteamSpy estimates (owners are a range, not a count)
owner_snapshots = Table(
    "owner_snapshots",
    metadata,
    Column("appid", Integer, primary_key=True),
    Column("date", Date, primary_key=True),
    Column("owners_low", BigInteger),
    Column("owners_high", BigInteger),
    Column("ccu", Integer),
    Column("positive", Integer),
    Column("negative", Integer),
    Column("avg_playtime_min", Integer),
    Column("median_playtime_min", Integer),
)

# Things that might move player counts: sales (derived from price changes),
# patches/announcements (Steam news feed), and manual annotations.
events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("appid", Integer, nullable=False, index=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("kind", String(32), nullable=False),  # sale_start|sale_end|sale_change|price_change|patch|announcement|manual
    Column("title", String(512)),
    Column("url", String(1024)),
    Column("detail", Text),
    Column("source_id", String(128), nullable=False),
    UniqueConstraint("appid", "kind", "source_id", name="uq_event_source"),
)

# Daily Steam review totals (storefront appdetails `recommendations.total`).
# New reviews per day = difference between consecutive days.
review_snapshots = Table(
    "review_snapshots",
    metadata,
    Column("appid", Integer, primary_key=True),
    Column("date", Date, primary_key=True),
    Column("total_reviews", BigInteger, nullable=False),
)

# Daily Steam review split (store.steampowered.com/appreviews query_summary).
# New positive/negative reviews per day = difference between consecutive days.
review_score_snapshots = Table(
    "review_score_snapshots",
    metadata,
    Column("appid", Integer, primary_key=True),
    Column("date", Date, primary_key=True),
    Column("total_positive", BigInteger, nullable=False),
    Column("total_negative", BigInteger, nullable=False),
    Column("review_score", Integer),
    Column("review_score_desc", String(64)),
)

# Games added or removed from the dashboard. Combined with games.toml:
# tracked = games.toml + active rows here - rows here with active = false.
# The dashboard's read-only role is granted INSERT/UPDATE on this table only.
tracked_games = Table(
    "tracked_games",
    metadata,
    Column("appid", Integer, primary_key=True),
    Column("name", String(255)),
    Column("active", Boolean, nullable=False, default=True),
    Column("added_at", DateTime),
    Column("updated_at", DateTime),
)

# Small key/value store for bookkeeping, e.g. which events were already alerted.
app_state = Table(
    "app_state",
    metadata,
    Column("key", String(128), primary_key=True),
    Column("value", String(512)),
    Column("updated_at", DateTime),
)

EVENT_KINDS = ("sale_start", "sale_end", "sale_change", "price_change", "patch", "announcement", "manual")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_engine(url: str | None = None, create_tables: bool = True) -> Engine:
    """create_tables=False for read-only clients (the dashboard's role can't run DDL)."""
    url = url or config.database_url()
    engine = create_engine(url, future=True, pool_pre_ping=True)
    if create_tables:
        metadata.create_all(engine)
    return engine


def _insert(engine: Engine, table: Table):
    """Dialect-specific INSERT so we can use ON CONFLICT."""
    if engine.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif engine.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported database dialect: {engine.dialect.name}")
    return insert(table)


def insert_ignore(engine: Engine, table: Table, rows: Iterable[dict[str, Any]]) -> int:
    """Insert rows, silently skipping primary/unique-key conflicts. Returns rows attempted."""
    rows = list(rows)
    if not rows:
        return 0
    stmt = _insert(engine, table).on_conflict_do_nothing()
    with engine.begin() as conn:
        conn.execute(stmt, rows)
    return len(rows)


def upsert(engine: Engine, table: Table, rows: Iterable[dict[str, Any]], keys: list[str]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    stmt = _insert(engine, table)
    update_cols = {c: stmt.excluded[c] for c in rows[0] if c not in keys}
    stmt = stmt.on_conflict_do_update(index_elements=keys, set_=update_cols)
    with engine.begin() as conn:
        conn.execute(stmt, rows)
    return len(rows)


def latest_price(engine: Engine, appid: int, country: str) -> dict[str, Any] | None:
    t = price_snapshots
    q = (
        select(t)
        .where(t.c.appid == appid, t.c.country == country)
        .order_by(t.c.ts.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(q).mappings().first()
    return dict(row) if row else None


def game_rows(engine: Engine) -> dict[int, dict[str, Any]]:
    with engine.connect() as conn:
        return {r["appid"]: dict(r) for r in conn.execute(select(games)).mappings()}


def tracked_appids(engine: Engine, config_appids: list[int]) -> list[int]:
    """games.toml list, minus games removed in the dashboard, plus games added there."""
    with engine.connect() as conn:
        rows = conn.execute(select(tracked_games.c.appid, tracked_games.c.active)
                            .order_by(tracked_games.c.added_at)).all()
    removed = {r.appid for r in rows if not r.active}
    added = [r.appid for r in rows if r.active]
    out = [a for a in config_appids if a not in removed]
    out += [a for a in added if a not in out]
    return out


def set_tracked(engine: Engine, appid: int, active: bool, name: str | None = None) -> None:
    now = utcnow()
    with engine.connect() as conn:
        existing = conn.execute(select(tracked_games).where(tracked_games.c.appid == appid)).mappings().first()
    row = {"appid": appid, "active": active, "updated_at": now,
           "name": name or (existing["name"] if existing else None),
           "added_at": existing["added_at"] if existing else now}
    upsert(engine, tracked_games, [row], ["appid"])


def has_review_snapshot(engine: Engine, appid: int, day) -> bool:
    t = review_snapshots
    with engine.connect() as conn:
        return conn.execute(select(t.c.appid).where(t.c.appid == appid, t.c.date == day)).first() is not None


def get_state(engine: Engine, key: str) -> str | None:
    with engine.connect() as conn:
        return conn.execute(select(app_state.c.value).where(app_state.c.key == key)).scalar()


def set_state(engine: Engine, key: str, value: str) -> None:
    upsert(engine, app_state, [{"key": key, "value": value, "updated_at": utcnow()}], ["key"])


def copy_database(src: Engine, dst: Engine, chunk: int = 2000) -> dict[str, int]:
    """Copy every row from src to dst (e.g. SQLite -> PostgreSQL). Safe to re-run:
    existing rows are skipped. Event ids are not copied, so dst's own id sequence
    stays consistent; events dedupe on (appid, kind, source_id)."""
    counts: dict[str, int] = {}
    for table in (games, player_snapshots, price_snapshots, owner_snapshots, review_snapshots,
                  review_score_snapshots, tracked_games, events):
        cols = [c for c in table.columns if not (table is events and c.name == "id")]
        n = 0
        with src.connect() as conn:
            result = conn.execute(select(*cols)).mappings()
            while batch := result.fetchmany(chunk):
                n += insert_ignore(dst, table, [dict(r) for r in batch])
        counts[table.name] = n
    return counts


def has_owner_snapshot(engine: Engine, appid: int, day) -> bool:
    t = owner_snapshots
    with engine.connect() as conn:
        return conn.execute(select(t.c.appid).where(t.c.appid == appid, t.c.date == day)).first() is not None


def has_snapshot(engine: Engine, table: Table, appid: int, day) -> bool:
    """Daily tables keyed by (appid, date)."""
    with engine.connect() as conn:
        return conn.execute(select(table.c.appid).where(table.c.appid == appid, table.c.date == day)).first() is not None

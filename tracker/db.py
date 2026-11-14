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

EVENT_KINDS = ("sale_start", "sale_end", "sale_change", "price_change", "patch", "announcement", "manual")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_engine(url: str | None = None) -> Engine:
    url = url or config.database_url()
    engine = create_engine(url, future=True, pool_pre_ping=True)
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


def copy_database(src: Engine, dst: Engine, chunk: int = 2000) -> dict[str, int]:
    """Copy every row from src to dst (e.g. SQLite -> PostgreSQL). Safe to re-run:
    existing rows are skipped. Event ids are not copied, so dst's own id sequence
    stays consistent; events dedupe on (appid, kind, source_id)."""
    counts: dict[str, int] = {}
    for table in (games, player_snapshots, price_snapshots, owner_snapshots, events):
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

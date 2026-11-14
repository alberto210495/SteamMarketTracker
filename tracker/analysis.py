"""Query + analysis helpers shared by the dashboard and the CLI report."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

from . import db


# --------------------------------------------------------------------------- loading

def _read(engine: Engine, stmt) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.DataFrame(conn.execute(stmt).mappings().all())


def load_games(engine: Engine) -> pd.DataFrame:
    df = _read(engine, select(db.games).order_by(db.games.c.name))
    if df.empty:
        return pd.DataFrame(columns=[c.name for c in db.games.columns])
    return df


def load_players(engine: Engine, appids: list[int], start: datetime | None = None,
                 end: datetime | None = None) -> pd.DataFrame:
    t = db.player_snapshots
    q = select(t).where(t.c.appid.in_(appids)).order_by(t.c.ts)
    if start:
        q = q.where(t.c.ts >= start)
    if end:
        q = q.where(t.c.ts <= end)
    df = _read(engine, q)
    if df.empty:
        return pd.DataFrame(columns=["appid", "ts", "players"])
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def load_prices(engine: Engine, appids: list[int], start: datetime | None = None,
                end: datetime | None = None) -> pd.DataFrame:
    t = db.price_snapshots
    q = select(t).where(t.c.appid.in_(appids)).order_by(t.c.ts)
    if start:
        q = q.where(t.c.ts >= start)
    if end:
        q = q.where(t.c.ts <= end)
    df = _read(engine, q)
    if df.empty:
        return pd.DataFrame(columns=[c.name for c in t.columns])
    df["ts"] = pd.to_datetime(df["ts"])
    df["final"] = df["final_cents"] / 100
    df["initial"] = df["initial_cents"] / 100
    return df


def load_events(engine: Engine, appids: list[int], kinds: list[str] | None = None) -> pd.DataFrame:
    t = db.events
    q = select(t).where(t.c.appid.in_(appids)).order_by(t.c.ts)
    if kinds:
        q = q.where(t.c.kind.in_(kinds))
    df = _read(engine, q)
    if df.empty:
        return pd.DataFrame(columns=[c.name for c in t.columns])
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def load_owners(engine: Engine, appids: list[int]) -> pd.DataFrame:
    t = db.owner_snapshots
    df = _read(engine, select(t).where(t.c.appid.in_(appids)).order_by(t.c.date))
    if df.empty:
        return pd.DataFrame(columns=[c.name for c in t.columns])
    df["date"] = pd.to_datetime(df["date"])
    return df


# --------------------------------------------------------------------------- analysis

def resample_players(players: pd.DataFrame, how: str) -> pd.DataFrame:
    """how: 'hourly' (raw), 'daily_peak', 'daily_avg'."""
    if players.empty or how == "hourly":
        return players
    agg = "max" if how == "daily_peak" else "mean"
    out = (players.set_index("ts").groupby("appid")["players"].resample("1D").agg(agg)
           .dropna().reset_index())
    out["players"] = out["players"].round().astype(int)
    return out


def with_gaps(df: pd.DataFrame, value_cols: list[str], max_gap: timedelta) -> pd.DataFrame:
    """Insert an all-NaN row inside every gap longer than `max_gap` (per game).

    Plotly breaks a line at NaN, so missed collection runs show up as a visible
    hole instead of a straight line that looks like real data.
    """
    if df.empty:
        return df
    parts = []
    for _, grp in df.sort_values("ts").groupby("appid", sort=False):
        gaps = grp["ts"].diff() > max_gap
        if gaps.any():
            filler = grp.loc[gaps, ["appid", "ts"]].copy()
            filler["ts"] = filler["ts"] - pd.Timedelta(seconds=1)
            for c in value_cols:
                filler[c] = float("nan")
            grp = pd.concat([grp, filler]).sort_values("ts")
        parts.append(grp)
    return pd.concat(parts, ignore_index=True)


def sale_intervals(events: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Pair sale_start with the next sale_end per game. Open sales run until `now`."""
    cols = ["appid", "start", "end", "discount_pct", "ongoing"]
    if events.empty:
        return pd.DataFrame(columns=cols)
    now = now or db.utcnow()
    rows = []
    for appid, grp in events[events["kind"].isin(["sale_start", "sale_end", "sale_change"])].groupby("appid"):
        start = None
        pct = 0
        for _, ev in grp.sort_values("ts").iterrows():
            detail = _detail(ev.get("detail"))
            if ev["kind"] == "sale_start":
                if start is None:
                    start, pct = ev["ts"], detail.get("discount_pct", 0)
            elif ev["kind"] == "sale_change" and start is not None:
                pct = max(pct, detail.get("discount_pct", pct))
            elif ev["kind"] == "sale_end" and start is not None:
                rows.append({"appid": appid, "start": start, "end": ev["ts"], "discount_pct": pct, "ongoing": False})
                start = None
        if start is not None:
            rows.append({"appid": appid, "start": start, "end": pd.Timestamp(now), "discount_pct": pct,
                         "ongoing": True})
    return pd.DataFrame(rows, columns=cols)


def _detail(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _window_daily_peaks(series: pd.Series, anchor: pd.Timestamp, days: int, before: bool) -> tuple[float | None, float]:
    """Mean of 24h-bucket peaks, buckets aligned to the event time.

    Aligning buckets to the event (not to midnight) means each bucket holds one
    full daily cycle, so the before/after comparison isn't biased by time of day.
    Returns (mean_peak, coverage) where coverage is the fraction of expected
    hourly samples present.
    """
    if before:
        lo, hi = anchor - timedelta(days=days), anchor
    else:
        lo, hi = anchor, anchor + timedelta(days=days)
    win = series[(series.index >= lo) & (series.index < hi)]
    coverage = len(win) / (days * 24)
    if win.empty:
        return None, 0.0
    bucket = ((win.index - lo) // timedelta(hours=24)).astype(int)
    peaks = win.groupby(bucket).max()
    return float(peaks.mean()), min(coverage, 1.0)


def _window_change(series: pd.Series, anchor: pd.Timestamp,
                   days: int) -> tuple[float | None, float | None, float | None, float, float]:
    b, cb = _window_daily_peaks(series, anchor, days, before=True)
    a, ca = _window_daily_peaks(series, anchor, days, before=False)
    change = (a - b) / b * 100 if (a is not None and b) else None
    return b, a, change, cb, ca


def event_impact(players: pd.DataFrame, events: pd.DataFrame, window_days: int = 7,
                 min_coverage: float = 0.6, market_players: pd.DataFrame | None = None,
                 min_market_games: int = 2) -> pd.DataFrame:
    """For each event: average daily-peak players in the N days before vs after.

    With `market_players` (all tracked games), each event is also compared with
    the *market*: the median change of the other games over the same windows.
    `vs_market_pct` is the game's change relative to that, so a Steam-wide sale
    that lifts everything doesn't look like this game's success:
        vs_market = (1 + change) / (1 + market_change) - 1

    Correlation, not causation. Rows lacking enough samples on either side are
    flagged with `enough_data=False` instead of being dropped.
    """
    cols = ["appid", "ts", "kind", "title", "before", "after", "change_pct", "coverage_before",
            "coverage_after", "enough_data", "market_change_pct", "market_games", "vs_market_pct"]
    if players.empty or events.empty:
        return pd.DataFrame(columns=cols)
    pool = market_players if market_players is not None else pd.DataFrame(columns=["appid", "ts", "players"])
    market_series = {a: g.set_index("ts")["players"].sort_index() for a, g in pool.groupby("appid")}
    out = []
    for appid, evs in events.groupby("appid"):
        series = players[players["appid"] == appid].set_index("ts")["players"].sort_index()
        if series.empty:
            continue
        for _, ev in evs.iterrows():
            b, a, change, cb, ca = _window_change(series, ev["ts"], window_days)
            others = []
            for other, s in market_series.items():
                if other == appid:
                    continue
                _, _, oc, ocb, oca = _window_change(s, ev["ts"], window_days)
                if oc is not None and ocb >= min_coverage and oca >= min_coverage:
                    others.append(oc)
            market = float(pd.Series(others).median()) if len(others) >= min_market_games else None
            vs_market = ((1 + change / 100) / (1 + market / 100) - 1) * 100 \
                if (change is not None and market is not None and market > -100) else None
            out.append({
                "appid": appid, "ts": ev["ts"], "kind": ev["kind"], "title": ev.get("title"),
                "before": b, "after": a, "change_pct": change,
                "coverage_before": cb, "coverage_after": ca,
                "enough_data": cb >= min_coverage and ca >= min_coverage,
                "market_change_pct": market, "market_games": len(others), "vs_market_pct": vs_market,
            })
    return pd.DataFrame(out, columns=cols).sort_values("ts", ascending=False, ignore_index=True)


def market_sale_periods(prices: pd.DataFrame, min_share: float = 0.5, min_games: int = 3,
                        max_gap: timedelta = timedelta(hours=3)) -> pd.DataFrame:
    """Periods when at least `min_share` of the tracked paid games (and at least
    `min_games` of them) were discounted at the same time - usually a Steam
    seasonal sale. Derived from the data, so no hard-coded sale calendar."""
    cols = ["start", "end", "share", "games_on_sale"]
    if prices.empty:
        return pd.DataFrame(columns=cols)
    paid = prices[~prices["is_free"].astype(bool)]
    if paid.empty:
        return pd.DataFrame(columns=cols)
    by_ts = paid.groupby("ts").agg(total=("appid", "nunique"),
                                   on_sale=("discount_pct", lambda d: int((d.fillna(0) > 0).sum())))
    by_ts["share"] = by_ts["on_sale"] / by_ts["total"]
    hits = by_ts[(by_ts["share"] >= min_share) & (by_ts["on_sale"] >= min_games)].sort_index()
    rows, start, prev, shares, counts = [], None, None, [], []
    for ts, r in hits.iterrows():
        if start is not None and ts - prev > max_gap:
            rows.append({"start": start, "end": prev, "share": max(shares), "games_on_sale": max(counts)})
            start, shares, counts = None, [], []
        if start is None:
            start = ts
        prev = ts
        shares.append(r["share"]); counts.append(int(r["on_sale"]))
    if start is not None:
        rows.append({"start": start, "end": prev, "share": max(shares), "games_on_sale": max(counts)})
    return pd.DataFrame(rows, columns=cols)


def to_timezone(df: pd.DataFrame, cols: list[str], tz: str) -> pd.DataFrame:
    """Stored timestamps are naive UTC; convert to naive local time for display."""
    if df.empty or tz in ("UTC", "Etc/UTC"):
        return df
    df = df.copy()
    for c in cols:
        if c in df and pd.api.types.is_datetime64_any_dtype(df[c]):
            df[c] = df[c].dt.tz_localize("UTC").dt.tz_convert(tz).dt.tz_localize(None)
    return df


def load_reviews(engine: Engine, appids: list[int]) -> pd.DataFrame:
    """Daily review totals plus `new_reviews` (per day, spread evenly over any gap)."""
    t = db.review_snapshots
    cols = ["appid", "date", "total_reviews", "new_reviews"]
    try:
        df = _read(engine, select(t).where(t.c.appid.in_(appids)).order_by(t.c.date))
    except (OperationalError, ProgrammingError):  # table not created yet
        return pd.DataFrame(columns=cols)
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["appid", "date"])
    days = df.groupby("appid")["date"].diff().dt.days
    df["new_reviews"] = df.groupby("appid")["total_reviews"].diff() / days
    return df


def load_tracked(engine: Engine) -> pd.DataFrame:
    t = db.tracked_games
    try:
        df = _read(engine, select(t).order_by(t.c.added_at))
    except (OperationalError, ProgrammingError):
        df = pd.DataFrame()
    return df if not df.empty else pd.DataFrame(columns=[c.name for c in t.columns])


def index_series(df: pd.DataFrame, value: str = "players") -> pd.DataFrame:
    """Rebase each game's series to 100 at its first point, so games of very
    different size share one axis when comparing trends."""
    if df.empty:
        return df.assign(index=pd.Series(dtype=float))
    df = df.sort_values("ts").copy()
    first = df.groupby("appid")[value].transform(lambda s: s[s > 0].iloc[0] if (s > 0).any() else float("nan"))
    df["index"] = df[value] / first * 100
    return df

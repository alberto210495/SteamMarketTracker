"""Query + analysis helpers shared by the dashboard and the CLI report."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

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


def event_impact(players: pd.DataFrame, events: pd.DataFrame, window_days: int = 7,
                 min_coverage: float = 0.6) -> pd.DataFrame:
    """For each event: average daily-peak players in the N days before vs after.

    Correlation, not causation - overlapping events, weekends and seasonal
    sales all move numbers too. Rows lacking enough samples on either side are
    flagged with `enough_data=False` instead of being dropped.
    """
    cols = ["appid", "ts", "kind", "title", "before", "after", "change_pct", "coverage_before",
            "coverage_after", "enough_data"]
    if players.empty or events.empty:
        return pd.DataFrame(columns=cols)
    out = []
    for appid, evs in events.groupby("appid"):
        series = players[players["appid"] == appid].set_index("ts")["players"].sort_index()
        if series.empty:
            continue
        for _, ev in evs.iterrows():
            b, cb = _window_daily_peaks(series, ev["ts"], window_days, before=True)
            a, ca = _window_daily_peaks(series, ev["ts"], window_days, before=False)
            change = (a - b) / b * 100 if (a is not None and b) else None
            out.append({
                "appid": appid, "ts": ev["ts"], "kind": ev["kind"], "title": ev.get("title"),
                "before": b, "after": a, "change_pct": change,
                "coverage_before": cb, "coverage_after": ca,
                "enough_data": cb >= min_coverage and ca >= min_coverage,
            })
    return pd.DataFrame(out, columns=cols).sort_values("ts", ascending=False, ignore_index=True)


def index_series(df: pd.DataFrame, value: str = "players") -> pd.DataFrame:
    """Rebase each game's series to 100 at its first point, so games of very
    different size share one axis when comparing trends."""
    if df.empty:
        return df.assign(index=pd.Series(dtype=float))
    df = df.sort_values("ts").copy()
    first = df.groupby("appid")[value].transform(lambda s: s[s > 0].iloc[0] if (s > 0).any() else float("nan"))
    df["index"] = df[value] / first * 100
    return df

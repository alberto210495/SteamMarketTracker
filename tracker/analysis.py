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


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def weekly_profile(players: pd.DataFrame) -> pd.DataFrame:
    """Average players by weekday x hour per game, plus `rel` = % of that game's
    overall average (100 = a normal hour). Timestamps are used as given, so pass
    them already converted to the viewer's time zone."""
    cols = ["appid", "weekday", "hour", "players", "samples", "rel"]
    if players.empty:
        return pd.DataFrame(columns=cols)
    df = players.dropna(subset=["players"]).copy()
    df["weekday"] = df["ts"].dt.dayofweek
    df["hour"] = df["ts"].dt.hour
    out = (df.groupby(["appid", "weekday", "hour"])["players"].agg(players="mean", samples="size")
           .reset_index())
    # Normalise by the mean of the cells (not of the samples) so missing hours don't skew it.
    out["rel"] = out["players"] / out.groupby("appid")["players"].transform("mean") * 100
    return out[cols]


def profile_summary(profile: pd.DataFrame) -> pd.DataFrame:
    """Busiest / quietest weekday-hour and the weekend effect, per game."""
    cols = ["appid", "peak_day", "peak_hour", "low_day", "low_hour", "peak_to_low", "weekend_pct"]
    if profile.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for appid, g in profile.groupby("appid"):
        hi, lo = g.loc[g["players"].idxmax()], g.loc[g["players"].idxmin()]
        weekend = g[g["weekday"] >= 5]["players"].mean()
        weekday = g[g["weekday"] < 5]["players"].mean()
        rows.append({"appid": appid, "peak_day": WEEKDAYS[int(hi["weekday"])], "peak_hour": int(hi["hour"]),
                     "low_day": WEEKDAYS[int(lo["weekday"])], "low_hour": int(lo["hour"]),
                     "peak_to_low": hi["players"] / lo["players"] if lo["players"] else None,
                     "weekend_pct": (weekend / weekday - 1) * 100 if weekday and pd.notna(weekend) else None})
    return pd.DataFrame(rows, columns=cols)


def _bucket_peaks(series: pd.Series, lo: pd.Timestamp, days: int) -> pd.Series:
    """Peak per 24h bucket starting at `lo` (index 0..days-1; missing buckets absent)."""
    win = series[(series.index >= lo) & (series.index < lo + timedelta(days=days))]
    if win.empty:
        return pd.Series(dtype=float)
    bucket = ((win.index - lo) // timedelta(hours=24)).astype(int)
    return win.groupby(bucket).max().astype(float)


def event_retention(players: pd.DataFrame, events: pd.DataFrame, baseline_days: int = 7,
                    max_days: int = 30, tolerance: float = 0.10, min_coverage: float = 0.6) -> pd.DataFrame:
    """Does a bump last? For each event: baseline = average daily peak in the
    `baseline_days` before; then the 24h-bucket peaks after the event are followed
    until they fall back to within `tolerance` of the baseline.

    status: 'no bump' (peak lift under tolerance), 'back to baseline',
    'still elevated' (not back yet, as of the latest data) or 'not enough data'.
    """
    cols = ["appid", "ts", "kind", "title", "baseline", "peak", "peak_lift_pct", "days_to_peak",
            "half_life_days", "days_to_baseline", "status"]
    if players.empty or events.empty:
        return pd.DataFrame(columns=cols)
    out = []
    for appid, evs in events.groupby("appid"):
        series = players[players["appid"] == appid].set_index("ts")["players"].sort_index()
        if series.empty:
            continue
        last = series.index.max()
        for _, ev in evs.iterrows():
            t = ev["ts"]
            before = _bucket_peaks(series, t - timedelta(days=baseline_days), baseline_days)
            after = _bucket_peaks(series, t, max_days)
            # Only full days count for "after"; the bucket still in progress would look like a drop.
            full_days = int((min(last, t + timedelta(days=max_days)) - t) // timedelta(hours=24))
            after = after[after.index < full_days]
            row = {"appid": appid, "ts": t, "kind": ev["kind"], "title": ev.get("title"), "baseline": None,
                   "peak": None, "peak_lift_pct": None, "days_to_peak": None, "half_life_days": None,
                   "days_to_baseline": None, "status": "not enough data"}
            if len(before) < baseline_days * min_coverage or after.empty or not before.mean():
                out.append(row)
                continue
            base = float(before.mean())
            first_week = after[after.index < 7]
            peak_day = int(first_week.idxmax())
            peak = float(first_week.max())
            lift = peak / base - 1
            row.update(baseline=base, peak=peak, peak_lift_pct=lift * 100, days_to_peak=peak_day)
            if lift < tolerance:
                row["status"] = "no bump"
                out.append(row)
                continue
            rest = after[after.index > peak_day]
            back = rest[rest <= base * (1 + tolerance)]
            half = rest[rest - base <= (peak - base) / 2]
            row["half_life_days"] = int(half.index[0] - peak_day) if not half.empty else None
            if not back.empty:
                row.update(days_to_baseline=int(back.index[0]), status="back to baseline")
            else:
                row.update(days_to_baseline=None, status="still elevated")
            out.append(row)
    return pd.DataFrame(out, columns=cols).sort_values("ts", ascending=False, ignore_index=True)


def sale_effectiveness(players: pd.DataFrame, events: pd.DataFrame, window_days: int = 7,
                       market_players: pd.DataFrame | None = None, now: datetime | None = None,
                       min_coverage: float = 0.6) -> pd.DataFrame:
    """How much each sale lifted players, per point of discount.

    The lift is the event-impact change at the sale's start (average daily peak in
    the N days before vs. the first N days of the sale, N capped at the sale's
    length). `vs_market_pct` is used when available, so a Steam-wide sale doesn't
    flatter every game. `lift_per_10pct` = lift / discount * 10, which shows where
    deeper discounts stop paying off.
    """
    cols = ["appid", "start", "end", "discount_pct", "duration_days", "ongoing", "before", "after",
            "change_pct", "vs_market_pct", "lift_pct", "lift_per_10pct", "enough_data"]
    sales = sale_intervals(events, now=now)
    sales = sales[sales["discount_pct"].fillna(0) > 0] if not sales.empty else sales
    if sales.empty or players.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for s in sales.itertuples():
        duration = (s.end - s.start).total_seconds() / 86400
        days = max(1, min(window_days, int(duration)))
        ev = pd.DataFrame([{"appid": s.appid, "ts": s.start, "kind": "sale_start", "title": ""}])
        imp = event_impact(players[players["appid"] == s.appid], ev, window_days=days,
                           min_coverage=min_coverage, market_players=market_players)
        if imp.empty:
            continue
        r = imp.iloc[0]
        lift = r["vs_market_pct"] if pd.notna(r["vs_market_pct"]) else r["change_pct"]
        rows.append({
            "appid": s.appid, "start": s.start, "end": s.end, "discount_pct": int(s.discount_pct),
            "duration_days": round(duration, 1), "ongoing": bool(s.ongoing), "before": r["before"],
            "after": r["after"], "change_pct": r["change_pct"], "vs_market_pct": r["vs_market_pct"],
            "lift_pct": lift, "lift_per_10pct": lift / s.discount_pct * 10 if pd.notna(lift) else None,
            "enough_data": bool(r["enough_data"]),
        })
    return pd.DataFrame(rows, columns=cols).sort_values("start", ascending=False, ignore_index=True)


def discount_brackets(effect: pd.DataFrame) -> pd.DataFrame:
    """Median lift by discount bracket, across all sales with enough data."""
    cols = ["bracket", "sales", "median_lift_pct", "median_lift_per_10pct"]
    ok = effect[effect["enough_data"] & effect["lift_pct"].notna()] if not effect.empty else effect
    if ok.empty:
        return pd.DataFrame(columns=cols)
    bins = [0, 24, 49, 74, 100]
    labels = ["1–24%", "25–49%", "50–74%", "75–100%"]
    b = pd.cut(ok["discount_pct"], bins=bins, labels=labels)
    out = ok.groupby(b, observed=True).agg(sales=("lift_pct", "size"), median_lift_pct=("lift_pct", "median"),
                                           median_lift_per_10pct=("lift_per_10pct", "median")).reset_index()
    return out.rename(columns={"discount_pct": "bracket"})[cols]


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

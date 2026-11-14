"""Streamlit dashboard.

    streamlit run dashboard/app.py              # real data (DATABASE_URL or data/steam.db)
    streamlit run dashboard/app.py -- --demo    # synthetic data from `python -m tracker demo`
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import analysis, config, db  # noqa: E402

st.set_page_config(page_title="Steam Market Tracker", page_icon="📈", layout="wide")

# --------------------------------------------------------------------------- theme
# Categorical slots in fixed order; a game keeps its color no matter what is filtered.
PALETTE = {
    "light": dict(series=["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
                  surface="#fcfcfb", ink="#0b0b0b", ink2="#52514e", muted="#898781", grid="#e1e0d9",
                  axis="#c3c2b7", up="#2a78d6", down="#e34948", neutral="#c3c2b7"),
    "dark": dict(series=["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"],
                 surface="#1a1a19", ink="#f0efec", ink2="#c3c2b7", muted="#898781", grid="#2c2c2a",
                 axis="#383835", up="#3987e5", down="#e66767", neutral="#383835"),
}
MAX_SERIES = len(PALETTE["light"]["series"])


def theme_mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


C = PALETTE[theme_mode()]


def style(fig: go.Figure, height: int = 340, y_title: str | None = None, legend: bool = True) -> go.Figure:
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=48 if legend else 12, b=8),
        paper_bgcolor=C["surface"], plot_bgcolor=C["surface"],
        font=dict(family='system-ui, -apple-system, "Segoe UI", sans-serif', color=C["ink2"], size=12),
        title_font=dict(color=C["ink"], size=14),
        hovermode="x unified",
        hoverlabel=dict(bgcolor=C["surface"], font_color=C["ink"], bordercolor=C["axis"]),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(color=C["ink2"])),
    )
    fig.update_xaxes(showgrid=False, linecolor=C["axis"], tickfont=dict(color=C["muted"]), automargin=True,
                     showspikes=True, spikemode="across", spikethickness=1, spikecolor=C["muted"], spikedash="dot")
    fig.update_yaxes(gridcolor=C["grid"], zeroline=False, linecolor=C["axis"], tickfont=dict(color=C["muted"]),
                     automargin=True, title_standoff=8,
                     title_text=y_title, title_font=dict(color=C["muted"]), rangemode="tozero")
    return fig


def show(fig: go.Figure, title: str | None = None) -> None:
    # Titles live in Streamlit, not Plotly, so they can never collide with the legend.
    if title:
        st.markdown(f"**{title}**")
    st.plotly_chart(fig, width="stretch", theme=None, config={"displaylogo": False})


def hex_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


# --------------------------------------------------------------------------- data

def _secret_db_url() -> str | None:
    """Streamlit Community Cloud passes credentials via st.secrets, not env vars."""
    try:
        return st.secrets.get("DATABASE_URL")
    except Exception:  # no secrets.toml locally
        return None


if not os.environ.get("DATABASE_URL") and _secret_db_url():
    os.environ["DATABASE_URL"] = _secret_db_url()

DEMO = "--demo" in sys.argv
DB_URL = f"sqlite:///{(config.ROOT / 'data' / 'demo.db').as_posix()}" if DEMO else config.database_url()


@st.cache_resource
def engine():
    return db.get_engine(DB_URL)


@st.cache_data(ttl=300)
def q_games() -> pd.DataFrame:
    return analysis.load_games(engine())


@st.cache_data(ttl=300)
def q_players(appids: tuple[int, ...]) -> pd.DataFrame:
    return analysis.load_players(engine(), list(appids))


@st.cache_data(ttl=300)
def q_prices(appids: tuple[int, ...]) -> pd.DataFrame:
    return analysis.load_prices(engine(), list(appids))


@st.cache_data(ttl=300)
def q_events(appids: tuple[int, ...]) -> pd.DataFrame:
    return analysis.load_events(engine(), list(appids))


@st.cache_data(ttl=300)
def q_owners(appids: tuple[int, ...]) -> pd.DataFrame:
    return analysis.load_owners(engine(), list(appids))


games = q_games()
if games.empty:
    st.title("Steam Market Tracker")
    st.info("No data yet. Run `python -m tracker collect` (or `python -m tracker demo` and "
            "`streamlit run dashboard/app.py -- --demo` for synthetic data).")
    st.stop()

games = games.sort_values("appid").reset_index(drop=True)
COLOR = {int(a): PALETTE[theme_mode()]["series"][i % MAX_SERIES] for i, a in enumerate(games["appid"])}
NAME = {int(r.appid): (r.name or f"App {r.appid}") for r in games.itertuples()}

# --------------------------------------------------------------------------- sidebar

with st.sidebar:
    st.header("Filters")
    if DEMO:
        st.warning("Showing **synthetic demo data**.")
    all_players = q_players(tuple(NAME))
    by_size = (all_players.sort_values("ts").groupby("appid")["players"].last()
               .reindex(list(NAME)).fillna(0).sort_values(ascending=False))
    default = [a for a in by_size.index[:4]]
    picked = st.multiselect("Games", options=list(by_size.index), default=default,
                            format_func=lambda a: NAME[a], max_selections=MAX_SERIES)
    range_label = st.radio("Time range", ["7 days", "30 days", "90 days", "All"], index=1, horizontal=True)
    resolution = st.radio("Player resolution", ["Auto", "Hourly", "Daily peak", "Daily average"], index=0,
                          help="Auto = hourly for 7 days (or while there is little data), daily peak otherwise. "
                               "Daily peak smooths out the day/night cycle.")
    window = st.slider("Impact window (days each side)", 1, 14, 7)
    show_news = st.checkbox("Show non-patch announcements on charts", value=False)
    st.caption(f"Data source: {'demo SQLite' if DEMO else engine().dialect.name}")

if not picked:
    st.info("Pick at least one game in the sidebar.")
    st.stop()

ids = tuple(sorted(int(a) for a in picked))
players = q_players(ids)
prices = q_prices(ids)
events = q_events(ids)
owners = q_owners(ids)

end = players["ts"].max() if not players.empty else pd.Timestamp(db.utcnow())
start = None if range_label == "All" else end - timedelta(days=int(range_label.split()[0]))


def in_range(df: pd.DataFrame, col: str = "ts") -> pd.DataFrame:
    return df if start is None or df.empty else df[df[col] >= start]


players_r = in_range(players)
prices_r = in_range(prices)
sales = analysis.sale_intervals(events, now=end)
marker_kinds = ["patch", "manual", "price_change"] + (["announcement"] if show_news else [])
markers = in_range(events[events["kind"].isin(marker_kinds)]) if not events.empty else events
if resolution == "Auto":
    few = players_r.empty or players_r["ts"].nunique() < 24 * 10
    resolution = "Hourly" if range_label == "7 days" or few else "Daily peak"
res_key = {"Hourly": "hourly", "Daily peak": "daily_peak", "Daily average": "daily_avg"}[resolution]
players_view = analysis.resample_players(players_r, res_key)
# Break lines where collection runs were missed (2+ hourly samples, or a whole day when resampled).
players_view = analysis.with_gaps(players_view, ["players"],
                                  timedelta(hours=2) if res_key == "hourly" else timedelta(days=1.5))
prices_view = analysis.with_gaps(prices_r, ["final", "initial", "discount_pct"], timedelta(hours=2))

# --------------------------------------------------------------------------- header

st.title("Steam Market Tracker")
if not players.empty:
    st.caption(f"Latest sample {end:%Y-%m-%d %H:%M} UTC · {len(NAME)} games tracked")


def latest_table() -> pd.DataFrame:
    rows = []
    for a in ids:
        p = players[players["appid"] == a].sort_values("ts")
        pr = prices[prices["appid"] == a].sort_values("ts")
        ow = owners[owners["appid"] == a].sort_values("date")
        now_n = int(p["players"].iloc[-1]) if not p.empty else None
        last = p["ts"].max() if not p.empty else None
        peak24 = int(p[p["ts"] > last - timedelta(hours=24)]["players"].max()) if last is not None else None
        prev_wk = p[(p["ts"] > last - timedelta(days=14)) & (p["ts"] <= last - timedelta(days=7))]["players"].max() \
            if last is not None else None
        cur_wk = p[p["ts"] > last - timedelta(days=7)]["players"].max() if last is not None else None
        wow = (cur_wk - prev_wk) / prev_wk * 100 if prev_wk and pd.notna(prev_wk) else None
        price = disc = None
        if not pr.empty:
            r = pr.iloc[-1]
            price = "Free" if r["is_free"] else f"{r['final']:.2f} {r['currency'] or ''}"
            disc = int(r["discount_pct"] or 0)
        own = None
        if not ow.empty and pd.notna(ow.iloc[-1]["owners_low"]):
            o = ow.iloc[-1]
            own = f"{int(o['owners_low']):,} – {int(o['owners_high']):,}"
        rows.append({"Game": NAME[a], "Players now": now_n, "24h peak": peak24, "7d peak vs prior 7d": wow,
                     "Price": price, "Discount": f"-{disc}%" if disc else "—", "Owners (SteamSpy)": own})
    return pd.DataFrame(rows)


st.dataframe(
    latest_table(), hide_index=True, width="stretch",
    column_config={
        "Players now": st.column_config.NumberColumn(format="localized"),
        "24h peak": st.column_config.NumberColumn(format="localized"),
        "7d peak vs prior 7d": st.column_config.NumberColumn(format="%+.1f%%"),
    },
)

tab_players, tab_prices, tab_impact, tab_compare, tab_owners, tab_data = st.tabs(
    ["Players", "Prices & sales", "Event impact", "Compare trends", "Owners & reviews", "Data"])

# --------------------------------------------------------------------------- players

with tab_players:
    st.caption("Shaded bands = discount active. Dotted lines = patches / annotations. "
               "Each game has its own scale.")
    for a in ids:
        d = players_view[players_view["appid"] == a]
        fig = go.Figure()
        for s in sales[sales["appid"] == a].itertuples():
            if start is not None and s.end < start:
                continue
            fig.add_vrect(x0=max(s.start, start) if start is not None else s.start, x1=s.end,
                          fillcolor=hex_rgba(COLOR[a], 0.12), line_width=0, layer="below",
                          annotation_text=f"-{s.discount_pct}%", annotation_position="top left",
                          annotation_font=dict(color=C["ink2"], size=11))
        if not d.empty:
            fig.add_trace(go.Scatter(x=d["ts"], y=d["players"], mode="lines", name=NAME[a],
                                     line=dict(color=COLOR[a], width=2),
                                     hovertemplate="%{y:,} players<extra></extra>"))
            ymax = d["players"].max()
            m = markers[markers["appid"] == a] if not markers.empty else markers
            if not m.empty:
                fig.add_trace(go.Scatter(
                    x=m["ts"], y=[ymax * 1.04] * len(m), mode="markers", name="Patches / events",
                    marker=dict(symbol="triangle-down", size=10, color=C["ink2"],
                                line=dict(color=C["surface"], width=2)),
                    text=m["title"], customdata=m["kind"],
                    hovertemplate="<b>%{customdata}</b>: %{text}<extra></extra>"))
                for t in m["ts"]:
                    fig.add_vline(x=t, line=dict(color=C["muted"], width=1, dash="dot"))
        else:
            fig.add_annotation(text="No player samples in range", showarrow=False, font=dict(color=C["muted"]))
        show(style(fig, height=300, y_title="Concurrent players", legend=False), title=NAME[a])

# --------------------------------------------------------------------------- prices

with tab_prices:
    if prices_r.empty:
        st.info("No price samples in range.")
    else:
        currencies = prices_r["currency"].dropna().unique()
        cur = currencies[0] if len(currencies) == 1 else "local currency"
        fig = go.Figure()
        for a in ids:
            d = prices_view[prices_view["appid"] == a]
            if d.empty:
                continue
            fig.add_trace(go.Scatter(
                x=d["ts"], y=d["final"], mode="lines", line=dict(color=COLOR[a], width=2, shape="hv"),
                name=NAME[a], customdata=d[["discount_pct", "initial"]],
                hovertemplate=NAME[a] + ": %{y:.2f} (−%{customdata[0]}% of %{customdata[1]:.2f})<extra></extra>"))
        show(style(fig, height=380, y_title=f"Price ({cur})"), title="Store price over time")

        sale_tbl = sales[sales["appid"].isin(ids)].copy()
        if not sale_tbl.empty:
            sale_tbl["Game"] = sale_tbl["appid"].map(NAME)
            sale_tbl["Duration (days)"] = ((sale_tbl["end"] - sale_tbl["start"]).dt.total_seconds() / 86400).round(1)
            st.subheader("Detected sales")
            st.dataframe(sale_tbl.sort_values("start", ascending=False)[
                ["Game", "start", "end", "discount_pct", "Duration (days)", "ongoing"]].rename(
                columns={"start": "Start (UTC)", "end": "End (UTC)", "discount_pct": "Max discount %",
                         "ongoing": "Ongoing"}), hide_index=True, width="stretch")

# --------------------------------------------------------------------------- impact

with tab_impact:
    st.caption(f"Average **daily peak** players in the {window} days before vs. after each event "
               "(24h buckets aligned to the event, so the day/night cycle cancels out). "
               "Correlation only: overlapping events, weekends and seasonal sales also move numbers.")
    kinds = st.multiselect("Event types", db.EVENT_KINDS, default=["sale_start", "patch", "manual"])
    ev = events[events["kind"].isin(kinds)] if not events.empty else events
    impact = analysis.event_impact(players, ev, window_days=window)
    if impact.empty:
        st.info("No events with player data around them yet.")
    else:
        impact["Game"] = impact["appid"].map(NAME)
        ok = impact[impact["enough_data"]].head(25).iloc[::-1]
        if not ok.empty:
            labels = ok["Game"] + " · " + ok["ts"].dt.strftime("%b %d") + " · " + ok["title"].fillna("").str.slice(0, 40)
            colors = [C["up"] if v >= 0 else C["down"] for v in ok["change_pct"]]
            fig = go.Figure(go.Bar(
                x=ok["change_pct"], y=labels, orientation="h", marker=dict(color=colors, cornerradius=4),
                customdata=ok[["before", "after", "kind"]],
                hovertemplate="%{customdata[2]}<br>%{customdata[0]:,.0f} → %{customdata[1]:,.0f} daily peak"
                              "<br><b>%{x:+.1f}%</b><extra></extra>"))
            fig.add_vline(x=0, line=dict(color=C["axis"], width=1))
            style(fig, height=max(260, 28 * len(ok) + 60), legend=False).update_layout(hovermode="closest")
            fig.update_xaxes(ticksuffix="%", showspikes=False, gridcolor=C["grid"], showgrid=True)
            fig.update_yaxes(showgrid=False, rangemode=None, tickfont=dict(color=C["ink2"]))
            show(fig, title="Change in average daily peak")
        st.dataframe(
            impact[["Game", "ts", "kind", "title", "before", "after", "change_pct", "enough_data"]].rename(columns={
                "ts": "When (UTC)", "kind": "Type", "title": "Event", "before": "Before (avg daily peak)",
                "after": "After (avg daily peak)", "change_pct": "Change", "enough_data": "Enough data"}),
            hide_index=True, width="stretch",
            column_config={
                "Before (avg daily peak)": st.column_config.NumberColumn(format="localized"),
                "After (avg daily peak)": st.column_config.NumberColumn(format="localized"),
                "Change": st.column_config.NumberColumn(format="%+.1f%%"),
            })

# --------------------------------------------------------------------------- compare

with tab_compare:
    st.caption("Daily peak players rebased to 100 at the start of the range, so games of any size "
               "share one axis. 150 = 50% above where it started.")
    daily = analysis.resample_players(players_r, "daily_peak")
    idx = analysis.with_gaps(analysis.index_series(daily), ["players", "index"], timedelta(days=1.5))
    if idx.empty:
        st.info("No player samples in range.")
    else:
        fig = go.Figure()
        ends = []
        for a in ids:
            d = idx[idx["appid"] == a]
            if d.empty:
                continue
            fig.add_trace(go.Scatter(x=d["ts"], y=d["index"], mode="lines", name=NAME[a],
                                     line=dict(color=COLOR[a], width=2), customdata=d["players"],
                                     hovertemplate=NAME[a] + ": %{y:.0f} (%{customdata:,} peak)<extra></extra>"))
            last = d.dropna(subset=["index"]).iloc[-1]
            ends.append([last["index"], last["ts"], NAME[a]])
        if len(ends) <= 4:  # direct labels at line ends, nudged apart so they never overlap
            vals = idx["index"].dropna()
            min_gap = (vals.max() - vals.min()) * 0.06 or 1
            ends.sort(key=lambda e: e[0])
            for i in range(1, len(ends)):
                ends[i][0] = max(ends[i][0], ends[i - 1][0] + min_gap)
            for y, x, name in ends:
                fig.add_annotation(x=x, y=y, text=name, xanchor="left", showarrow=False, xshift=6,
                                   font=dict(color=C["ink2"], size=11))
        fig.add_hline(y=100, line=dict(color=C["axis"], width=1, dash="dot"))
        style(fig, height=420, y_title="Index (start = 100)")
        fig.update_yaxes(rangemode="normal")
        fig.update_layout(margin=dict(r=170 if len(ids) <= 4 else 8))
        show(fig, title="Relative player trend")

# --------------------------------------------------------------------------- owners

with tab_owners:
    if owners.empty:
        st.info("No SteamSpy data yet (fetched once per day).")
    else:
        st.caption("SteamSpy owner counts are coarse estimate *ranges*; a jump usually means the estimate "
                   "moved up a bracket, not a sudden surge. Review share is more granular.")
        cols = st.columns(2)
        with cols[0]:
            fig = go.Figure()
            for a in ids:
                d = owners[owners["appid"] == a].dropna(subset=["owners_low"])
                if d.empty:
                    continue
                mid = (d["owners_low"] + d["owners_high"]) / 2
                fig.add_trace(go.Scatter(x=d["date"], y=mid, mode="lines", name=NAME[a],
                                         line=dict(color=COLOR[a], width=2, shape="hv"),
                                         customdata=d[["owners_low", "owners_high"]],
                                         hovertemplate=NAME[a] + ": %{customdata[0]:,} – %{customdata[1]:,}"
                                                                 "<extra></extra>"))
            style(fig, height=360, y_title="Owners (bracket midpoint)")
            fig.update_yaxes(type="log", rangemode="normal", tickformat="~s")
            show(fig, title="Owner estimate (log scale)")
        with cols[1]:
            fig = go.Figure()
            for a in ids:
                d = owners[owners["appid"] == a].dropna(subset=["positive", "negative"])
                d = d[(d["positive"] + d["negative"]) > 0]
                if d.empty:
                    continue
                share = d["positive"] / (d["positive"] + d["negative"]) * 100
                fig.add_trace(go.Scatter(x=d["date"], y=share, mode="lines", name=NAME[a],
                                         line=dict(color=COLOR[a], width=2), customdata=d["positive"] + d["negative"],
                                         hovertemplate=NAME[a] + ": %{y:.1f}% positive (%{customdata:,} reviews)"
                                                                 "<extra></extra>"))
            style(fig, height=360, y_title="% positive reviews")
            fig.update_yaxes(rangemode="normal", ticksuffix="%")
            show(fig, title="Review score")

# --------------------------------------------------------------------------- raw data

with tab_data:
    choice = st.radio("Table", ["Players", "Prices", "Events", "Owners"], horizontal=True)
    df = {"Players": players_r, "Prices": prices_r, "Events": in_range(events) if not events.empty else events,
          "Owners": owners}[choice].copy()
    if "appid" in df and not df.empty:
        df.insert(0, "game", df["appid"].map(NAME))
    st.dataframe(df, hide_index=True, width="stretch")
    st.download_button("Download CSV", df.to_csv(index=False).encode(), file_name=f"{choice.lower()}.csv",
                       mime="text/csv")

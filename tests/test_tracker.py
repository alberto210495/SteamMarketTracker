import os
from datetime import datetime, timedelta

import pandas as pd
import pytest

from tracker import analysis, collect, config, db
from tracker.steam import PriceInfo, classify_news, parse_owner_range, parse_price_entry


# --------------------------------------------------------------------------- parsers

def test_parse_price_paid():
    entry = {"success": True, "data": {"price_overview": {
        "currency": "USD", "initial": 5999, "final": 2999, "discount_percent": 50}}}
    p = parse_price_entry(entry)
    assert p == PriceInfo(False, "USD", 5999, 2999, 50)


def test_parse_price_free_game_returns_empty_list():
    assert parse_price_entry({"success": True, "data": []}).is_free is True


def test_parse_price_failure():
    assert parse_price_entry({"success": False}) is None
    assert parse_price_entry(None) is None


@pytest.mark.parametrize("raw,expected", [
    ("20,000,000 .. 50,000,000", (20_000_000, 50_000_000)),
    ("0 .. 20,000", (0, 20_000)),
    ("", (None, None)),
    (None, (None, None)),
])
def test_owner_range(raw, expected):
    assert parse_owner_range(raw) == expected


@pytest.mark.parametrize("item,kind", [
    ({"title": "Weekly update", "tags": []}, "patch"),
    ({"title": "Anything", "tags": ["patchnotes"]}, "patch"),
    ({"title": "Hotfix for crash on launch"}, "patch"),
    ({"title": "v1.4.2 is live"}, "patch"),
    ({"title": "Join our community tournament!"}, "announcement"),
    ({"title": "Community Update #36 A Very Moddy Christmas"}, "announcement"),
])
def test_classify_news(item, kind):
    assert classify_news(item) == kind


# --------------------------------------------------------------------------- sale detection

def snap(discount=0, initial=2000, final=None, currency="USD", free=False):
    return {"discount_pct": discount, "initial_cents": initial,
            "final_cents": final if final is not None else initial * (100 - discount) // 100,
            "currency": currency, "is_free": free}


def price(discount=0, initial=2000, currency="USD", free=False):
    return PriceInfo(free, currency, initial, initial * (100 - discount) // 100, discount)


def kinds(evts):
    return [k for k, _, _ in evts]


def test_sale_start_end_change():
    assert kinds(collect.detect_price_events(snap(0), price(50))) == ["sale_start"]
    assert kinds(collect.detect_price_events(snap(50), price(0))) == ["sale_end"]
    assert kinds(collect.detect_price_events(snap(25), price(50))) == ["sale_change"]
    assert collect.detect_price_events(snap(50), price(50)) == []


def test_first_snapshot_only_flags_running_sale():
    assert collect.detect_price_events(None, price(0)) == []
    assert kinds(collect.detect_price_events(None, price(40))) == ["sale_start"]


def test_base_price_change():
    assert kinds(collect.detect_price_events(snap(0, initial=2000), price(0, initial=2500))) == ["price_change"]
    # currency switch is not a price change
    assert collect.detect_price_events(snap(0, initial=2000, currency="EUR"), price(0, initial=2500)) == []


def test_went_free():
    assert kinds(collect.detect_price_events(snap(0), price(0, initial=0, free=True))) == ["price_change"]


# --------------------------------------------------------------------------- end-to-end collect with fake client

class FakeClient:
    def __init__(self):
        self.discount = 0

    def current_players(self, appid):
        return 1000 + appid

    def prices(self, appids, country):
        mult = {"us": 1, "gb": 0.8}.get(country, 1)
        return {a: price(self.discount, initial=int(2000 * mult), currency="USD" if country == "us" else "GBP")
                for a in appids}

    def app_details(self, appid, country):
        return {"name": f"Game {appid}", "is_free": False, "developers": ["Dev"], "publishers": ["Pub"],
                "release_date": {"date": "1 Jan, 2024"}, "recommendations": {"total": 5000 + appid}}

    def news(self, appid):
        return [{"gid": "123", "title": "Patch 1.1", "url": "https://x", "date": 1_700_000_000, "tags": []}]

    def followers(self, appid):
        return 10_000 + appid

    def review_summary(self, appid):
        return {"total_positive": 900 + appid, "total_negative": 100, "review_score": 8,
                "review_score_desc": "Very Positive"}

    def steamspy(self, appid):
        return {"appid": appid, "owners": "1,000 .. 2,000", "positive": 10, "negative": 2, "ccu": 5,
                "average_forever": 60, "median_forever": 30}


@pytest.fixture
def engine(tmp_path):
    """SQLite by default; set TEST_DATABASE_URL to run the same tests on PostgreSQL.

    The Postgres database is wiped before and after each test - never point
    this at real data.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        yield db.get_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
        return
    eng = db.get_engine(url)
    db.metadata.drop_all(eng)
    db.metadata.create_all(eng)
    yield eng
    db.metadata.drop_all(eng)
    eng.dispose()


def test_collect_run_idempotent_and_detects_sale(engine, monkeypatch):
    client = FakeClient()
    games = config.GamesConfig(country="us", appids=[1, 2])
    t0 = datetime(2026, 1, 1, 10)
    monkeypatch.setattr(db, "utcnow", lambda: t0)

    stats = collect.run(engine=engine, client=client, games=games)
    assert not stats.failures
    assert stats.players == 2 and stats.prices == 2 and stats.owners == 2 and stats.metadata == 2

    # Same hour again: nothing new
    stats = collect.run(engine=engine, client=client, games=games)
    assert stats.prices == 0 and stats.owners == 0 and stats.metadata == 0

    # Next hour a 50% sale starts
    client.discount = 50
    monkeypatch.setattr(db, "utcnow", lambda: t0 + timedelta(hours=1))
    stats = collect.run(engine=engine, client=client, games=games)
    assert stats.events == 2

    ev = analysis.load_events(engine, [1, 2])
    assert sorted(ev["kind"].tolist()) == ["patch", "patch", "sale_start", "sale_start"]
    assert len(analysis.load_players(engine, [1, 2])) == 4
    assert analysis.load_games(engine)["name"].tolist() == ["Game 1", "Game 2"]


# --------------------------------------------------------------------------- analysis

def hourly(appid, start, days, fn):
    ts = pd.date_range(start, periods=days * 24, freq="h")
    return pd.DataFrame({"appid": appid, "ts": ts, "players": [fn(t) for t in ts]})


def test_event_impact_measures_daily_peak_change():
    event_ts = pd.Timestamp("2026-03-10 17:00")
    # Daily cycle peaking at 1000 before, 1500 after
    def f(t):
        base = 1000 if t < event_ts else 1500
        return int(base * (0.5 + 0.5 * ((t.hour % 24) / 23)))
    players = hourly(1, "2026-03-01", 20, f)
    events = pd.DataFrame([{"appid": 1, "ts": event_ts, "kind": "sale_start", "title": "50% off"}])
    out = analysis.event_impact(players, events, window_days=7)
    row = out.iloc[0]
    assert row["enough_data"]
    assert row["before"] == pytest.approx(1000)
    assert row["after"] == pytest.approx(1500)
    assert row["change_pct"] == pytest.approx(50)


def test_event_impact_flags_missing_data():
    players = hourly(1, "2026-03-01", 3, lambda t: 100)
    events = pd.DataFrame([{"appid": 1, "ts": pd.Timestamp("2026-03-03 12:00"), "kind": "patch", "title": "p"}])
    out = analysis.event_impact(players, events, window_days=7)
    assert not out.iloc[0]["enough_data"]


def test_sale_intervals_pairs_and_open_sale():
    ev = pd.DataFrame([
        {"appid": 1, "ts": pd.Timestamp("2026-01-01"), "kind": "sale_start", "detail": '{"discount_pct": 50}'},
        {"appid": 1, "ts": pd.Timestamp("2026-01-08"), "kind": "sale_end", "detail": "{}"},
        {"appid": 1, "ts": pd.Timestamp("2026-02-01"), "kind": "sale_start", "detail": '{"discount_pct": 25}'},
    ])
    out = analysis.sale_intervals(ev, now=datetime(2026, 2, 5))
    assert len(out) == 2
    assert out.iloc[0]["discount_pct"] == 50 and not out.iloc[0]["ongoing"]
    assert out.iloc[1]["ongoing"] and out.iloc[1]["end"] == pd.Timestamp("2026-02-05")


def test_index_series_rebases_to_100():
    df = pd.DataFrame({"appid": [1, 1, 2, 2], "ts": pd.to_datetime(["2026-01-01", "2026-01-02"] * 2),
                       "players": [200, 300, 10, 5]})
    out = analysis.index_series(df)
    assert out[out.appid == 1]["index"].tolist() == [100, 150]
    assert out[out.appid == 2]["index"].tolist() == [100, 50]


def test_with_gaps_inserts_break_only_in_long_gaps():
    ts = pd.to_datetime(["2026-01-01 00:00", "2026-01-01 01:00", "2026-01-01 02:00",  # contiguous
                         "2026-01-01 08:00", "2026-01-01 09:00"])                       # 6h hole
    df = pd.DataFrame({"appid": 1, "ts": ts, "players": [1, 2, 3, 4, 5]})
    out = analysis.with_gaps(df, ["players"], timedelta(hours=2))
    assert len(out) == 6
    nan_rows = out[out["players"].isna()]
    assert len(nan_rows) == 1
    assert pd.Timestamp("2026-01-01 02:00") < nan_rows["ts"].iloc[0] < pd.Timestamp("2026-01-01 08:00")
    assert out["players"].dropna().tolist() == [1, 2, 3, 4, 5]


def test_copy_database_is_complete_and_rerunnable(tmp_path, engine):
    """SQLite -> `engine` (another SQLite file, or PostgreSQL when TEST_DATABASE_URL is set)."""
    from tracker import demo
    src = db.get_engine(f"sqlite:///{(tmp_path / 'src.db').as_posix()}")
    dst = engine
    demo.generate(src, days=30)
    first = db.copy_database(src, dst)
    db.copy_database(src, dst)  # second run must not duplicate or fail
    for table in (db.games, db.player_snapshots, db.price_snapshots, db.owner_snapshots, db.events):
        with src.connect() as a, dst.connect() as b:
            n_src = a.execute(db.select(db.func.count()).select_from(table)).scalar()
            n_dst = b.execute(db.select(db.func.count()).select_from(table)).scalar()
        assert n_src == n_dst == first[table.name] > 0
    # destination still accepts new events (its id sequence wasn't disturbed)
    db.insert_ignore(dst, db.events, [{"appid": 1, "ts": datetime(2026, 1, 1), "kind": "manual",
                                       "title": "x", "url": None, "detail": "{}", "source_id": "new"}])


# --------------------------------------------------------------------------- alerts

from tracker import alerts as alerts_mod  # noqa: E402
from tracker.config import AlertsConfig  # noqa: E402


class FakeSender:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def __call__(self, embeds):
        self.calls.append(embeds)
        return self.ok

    @property
    def titles(self):
        return [e["title"] for call in self.calls for e in call]


def _seed_game(engine, appid=1, name="Game 1"):
    db.upsert(engine, db.games, [{"appid": appid, "name": name}], ["appid"])


def _event(engine, appid, ts, kind, title, source):
    db.insert_ignore(engine, db.events, [{"appid": appid, "ts": ts, "kind": kind, "title": title,
                                           "url": None, "detail": "{}", "source_id": source}])


NO_EXTRAS = AlertsConfig(spikes=False, daily_summary=False)


def test_first_alert_run_does_not_flood_then_sends_only_new_events(engine):
    now = datetime(2026, 3, 1, 12)
    _seed_game(engine)
    _event(engine, 1, now - timedelta(hours=3), "patch", "Old patch", "a")
    send = FakeSender()
    alerts_mod.process(engine, NO_EXTRAS, [1], send, now=now)
    assert send.calls == []  # first run only initialises the cursor

    _event(engine, 1, now, "sale_start", "50% off", "b")
    _event(engine, 1, now, "announcement", "Tournament!", "c")       # not an alert kind
    _event(engine, 1, now - timedelta(days=30), "patch", "Ancient", "d")  # too old to announce
    alerts_mod.process(engine, NO_EXTRAS, [1], send, now=now)
    assert send.titles == ["On sale: Game 1"]

    alerts_mod.process(engine, NO_EXTRAS, [1], send, now=now)  # nothing new -> nothing sent
    assert len(send.calls) == 1


def test_failed_send_is_retried_next_run(engine):
    now = datetime(2026, 3, 1, 12)
    _seed_game(engine)
    _event(engine, 1, now, "patch", "seed", "a")
    alerts_mod.process(engine, NO_EXTRAS, [1], FakeSender(), now=now)  # init cursor
    _event(engine, 1, now, "patch", "Patch 1.1", "b")
    alerts_mod.process(engine, NO_EXTRAS, [1], FakeSender(ok=False), now=now)
    retry = FakeSender()
    alerts_mod.process(engine, NO_EXTRAS, [1], retry, now=now)
    assert retry.titles == ["New patch: Game 1"]


def test_disabled_kinds_are_skipped(engine):
    now = datetime(2026, 3, 1, 12)
    _seed_game(engine)
    _event(engine, 1, now, "patch", "seed", "a")
    cfg = AlertsConfig(sales=False, spikes=False, daily_summary=False)
    alerts_mod.process(engine, cfg, [1], FakeSender(), now=now)
    _event(engine, 1, now, "sale_start", "50% off", "b")
    _event(engine, 1, now, "patch", "Patch 2", "c")
    send = FakeSender()
    alerts_mod.process(engine, cfg, [1], send, now=now)
    assert send.titles == ["New patch: Game 1"]


@pytest.mark.parametrize("latest,history,expected", [
    (3000, [1000] * 7, 1000),       # 3x usual -> spike
    (1300, [1000] * 7, None),       # 1.3x -> no
    (299, [100] * 7, None),         # ~3x but only +199 (needs at least +200)
    (300, [100] * 7, 100),          # exactly +200 -> spike
    (5000, [1000] * 3, None),       # not enough history
])
def test_detect_spike(latest, history, expected):
    assert alerts_mod.detect_spike(latest, history, AlertsConfig()) == expected


def test_spike_alert_with_cooldown(engine):
    now = datetime(2026, 3, 15, 20)
    _seed_game(engine)
    rows = [{"appid": 1, "ts": now - timedelta(days=d), "players": 1000} for d in range(1, 10)]
    rows.append({"appid": 1, "ts": now, "players": 4000})
    db.insert_ignore(engine, db.player_snapshots, rows)
    cfg = AlertsConfig(sales=False, patches=False, daily_summary=False)
    send = FakeSender()
    alerts_mod.process(engine, cfg, [1], send, now=now)
    assert send.titles == ["Player spike: Game 1"]
    alerts_mod.process(engine, cfg, [1], send, now=now + timedelta(hours=1))  # cooldown
    assert len(send.calls) == 1


def test_daily_summary_once_per_day_after_hour(engine):
    _seed_game(engine)
    base = datetime(2026, 3, 1, 0)
    db.insert_ignore(engine, db.player_snapshots,
                     [{"appid": 1, "ts": base + timedelta(hours=h), "players": 100 + h} for h in range(40)])
    cfg = AlertsConfig(sales=False, patches=False, spikes=False, daily_summary_hour_utc=14)
    send = FakeSender()
    alerts_mod.process(engine, cfg, [1], send, now=datetime(2026, 3, 2, 13))   # before the hour
    assert send.calls == []
    alerts_mod.process(engine, cfg, [1], send, now=datetime(2026, 3, 2, 15))
    alerts_mod.process(engine, cfg, [1], send, now=datetime(2026, 3, 2, 16))   # same day again
    assert len(send.calls) == 1 and send.titles[0].startswith("Daily summary")
    assert "Game 1" in send.calls[0][0]["description"]


def test_collect_run_triggers_alerts(engine, monkeypatch):
    t0 = datetime(2026, 1, 1, 10)
    monkeypatch.setattr(db, "utcnow", lambda: t0)
    client, games = FakeClient(), config.GamesConfig(country="us", appids=[1],
                                                       alerts=AlertsConfig(spikes=False, daily_summary=False))
    send = FakeSender()
    collect.run(engine=engine, client=client, games=games, send_alerts=send)   # init
    client.discount = 50
    monkeypatch.setattr(db, "utcnow", lambda: t0 + timedelta(hours=1))
    collect.run(engine=engine, client=client, games=games, send_alerts=send)
    assert send.titles == ["On sale: Game 1"]


# --------------------------------------------------------------------------- tracked games, reviews

def test_tracked_appids_merges_config_and_dashboard(engine):
    assert db.tracked_appids(engine, [1, 2, 3]) == [1, 2, 3]
    db.set_tracked(engine, 9, True, name="New Game")     # added in the dashboard
    db.set_tracked(engine, 2, False)                      # config game removed in the dashboard
    assert db.tracked_appids(engine, [1, 2, 3]) == [1, 3, 9]
    db.set_tracked(engine, 2, True)                       # re-added
    db.set_tracked(engine, 9, False)
    assert db.tracked_appids(engine, [1, 2, 3]) == [1, 2, 3]
    assert analysis.load_tracked(engine).set_index("appid").loc[9, "name"] == "New Game"


def test_collector_uses_dashboard_additions_and_collects_reviews_daily(engine, monkeypatch):
    t0 = datetime(2026, 1, 1, 10)
    monkeypatch.setattr(db, "utcnow", lambda: t0)
    db.set_tracked(engine, 7, True)
    games = config.GamesConfig(country="us", appids=[1])
    stats = collect.run(engine=engine, client=FakeClient(), games=games)
    assert stats.players == 2 and stats.reviews == 2
    assert collect.run(engine=engine, client=FakeClient(), games=games).reviews == 0  # once per day
    monkeypatch.setattr(db, "utcnow", lambda: t0 + timedelta(days=1))
    assert collect.run(engine=engine, client=FakeClient(), games=games).reviews == 2


def test_parse_review_total():
    from tracker.steam import parse_review_total
    assert parse_review_total({"recommendations": {"total": 477533}}) == 477533
    assert parse_review_total({}) is None and parse_review_total(None) is None


def test_load_reviews_new_per_day(engine):
    db.insert_ignore(engine, db.review_snapshots, [
        {"appid": 1, "date": datetime(2026, 1, 1).date(), "total_reviews": 1000},
        {"appid": 1, "date": datetime(2026, 1, 2).date(), "total_reviews": 1100},
        {"appid": 1, "date": datetime(2026, 1, 5).date(), "total_reviews": 1400},  # 3-day gap
    ])
    df = analysis.load_reviews(engine, [1])
    assert df["new_reviews"].tolist()[1:] == [100, 100]


# --------------------------------------------------------------------------- market & time zone

def test_market_sale_periods_detects_shared_discounts():
    ts = pd.date_range("2026-06-01", periods=10, freq="h")
    rows = []
    for a in range(1, 5):
        for i, t in enumerate(ts):
            on_sale = 3 <= i <= 6 and a <= 3           # 3 of 4 games discounted for 4 hours
            rows.append({"appid": a, "ts": t, "discount_pct": 50 if on_sale else 0, "is_free": False})
    rows += [{"appid": 99, "ts": t, "discount_pct": 0, "is_free": True} for t in ts]  # free games ignored
    out = analysis.market_sale_periods(pd.DataFrame(rows))
    assert len(out) == 1
    assert out.iloc[0]["start"] == ts[3] and out.iloc[0]["end"] == ts[6]
    assert out.iloc[0]["games_on_sale"] == 3


def test_event_impact_vs_market():
    event_ts = pd.Timestamp("2026-03-10 17:00")
    def make(appid, after_mult):
        return hourly(appid, "2026-03-01", 20, lambda t: int(1000 * (after_mult if t >= event_ts else 1)))
    players = pd.concat([make(1, 1.5), make(2, 1.2), make(3, 1.2), make(4, 1.2)])
    events = pd.DataFrame([{"appid": 1, "ts": event_ts, "kind": "sale_start", "title": "x"}])
    row = analysis.event_impact(players[players.appid == 1], events, window_days=7, market_players=players).iloc[0]
    assert row["change_pct"] == pytest.approx(50)
    assert row["market_change_pct"] == pytest.approx(20)
    assert row["vs_market_pct"] == pytest.approx(25)       # 1.5 / 1.2 - 1
    assert row["market_games"] == 3


def test_event_impact_market_needs_enough_games():
    event_ts = pd.Timestamp("2026-03-10 17:00")
    players = pd.concat([hourly(a, "2026-03-01", 20, lambda t: 100) for a in (1, 2)])
    events = pd.DataFrame([{"appid": 1, "ts": event_ts, "kind": "patch", "title": "x"}])
    row = analysis.event_impact(players, events, market_players=players).iloc[0]
    assert pd.isna(row["vs_market_pct"]) and row["market_games"] == 1


def test_to_timezone_converts_naive_utc():
    df = pd.DataFrame({"ts": pd.to_datetime(["2026-01-15 18:00", "2026-07-15 18:00"])})
    out = analysis.to_timezone(df, ["ts"], "America/Chicago")
    assert out["ts"].tolist() == [pd.Timestamp("2026-01-15 12:00"), pd.Timestamp("2026-07-15 13:00")]  # CST / CDT
    assert analysis.to_timezone(df, ["ts"], "UTC") is df


# --------------------------------------------------------------------------- sale effectiveness

def test_sale_effectiveness_lift_per_discount():
    sale_ts = pd.Timestamp("2026-03-10 17:00")
    players = hourly(1, "2026-03-01", 20, lambda t: 1500 if t >= sale_ts else 1000)
    events = pd.DataFrame([
        {"appid": 1, "ts": sale_ts, "kind": "sale_start", "title": "50% off", "detail": '{"discount_pct": 50}'},
        {"appid": 1, "ts": sale_ts + timedelta(days=7), "kind": "sale_end", "title": "end", "detail": "{}"},
    ])
    out = analysis.sale_effectiveness(players, events, window_days=7, now=datetime(2026, 3, 21))
    row = out.iloc[0]
    assert row["discount_pct"] == 50 and row["enough_data"]
    assert row["lift_pct"] == pytest.approx(50)          # no market data -> raw change
    assert row["lift_per_10pct"] == pytest.approx(10)
    brackets = analysis.discount_brackets(out)
    assert brackets.iloc[0]["bracket"] == "50–74%" and brackets.iloc[0]["sales"] == 1


def test_event_retention_half_life_and_return():
    ev_ts = pd.Timestamp("2026-03-10 00:00")
    # +100% bump that decays by 20 points a day: 2000, 1800, ... back near 1000 on day 5
    def f(t):
        if t < ev_ts:
            return 1000
        d = (t - ev_ts).days
        return int(1000 + max(0, 1000 - 200 * d))
    players = hourly(1, "2026-03-01", 25, f)
    events = pd.DataFrame([{"appid": 1, "ts": ev_ts, "kind": "patch", "title": "p"},
                           {"appid": 1, "ts": pd.Timestamp("2026-03-20"), "kind": "manual", "title": "m"}])
    out = analysis.event_retention(players, events, baseline_days=7).set_index("kind")
    r = out.loc["patch"]
    assert r["peak_lift_pct"] == pytest.approx(100) and r["days_to_peak"] == 0
    assert r["half_life_days"] == 3          # 1400 is the first day within half of the +1000 gain
    assert r["days_to_baseline"] == 5 and r["status"] == "back to baseline"
    assert out.loc["manual", "status"] == "no bump"


def test_weekly_profile_and_summary():
    # Busy at 20:00 every day, twice as busy on weekends
    players = hourly(1, "2026-03-02", 14, lambda t: (200 if t.hour == 20 else 100) * (2 if t.weekday() >= 5 else 1))
    prof = analysis.weekly_profile(players)
    assert len(prof) == 7 * 24 and prof["samples"].eq(2).all()
    assert prof["rel"].mean() == pytest.approx(100)
    s = analysis.profile_summary(prof).iloc[0]
    assert s["peak_hour"] == 20 and s["peak_day"] in ("Sat", "Sun")
    assert s["weekend_pct"] == pytest.approx(100)


def test_seasonal_sale_calendar():
    sales = {s["name"]: s["start"] for s in analysis.steam_seasonal_sales(2025)}
    assert sales["Spring Sale"].date() == datetime(2025, 3, 13).date()   # actual: Mar 13
    assert sales["Summer Sale"].date() == datetime(2025, 6, 26).date()   # actual: Jun 26
    assert sales["Winter Sale"].date() == datetime(2025, 12, 18).date()  # actual: Dec 18
    nxt = analysis.next_seasonal_sale(datetime(2026, 11, 16))
    assert nxt["name"] == "Autumn Sale" and nxt["start"] == datetime(2026, 11, 25, 17) and not nxt["ongoing"]


def test_predict_next_sales_from_regular_gaps():
    rows = []
    for day in [1, 31, 61, 91]:   # every 30 days, a week long
        t = pd.Timestamp("2026-01-01") + pd.Timedelta(days=day)
        rows += [{"appid": 1, "ts": t, "kind": "sale_start", "detail": '{"discount_pct": 50}'},
                 {"appid": 1, "ts": t + pd.Timedelta(days=7), "kind": "sale_end", "detail": "{}"}]
    out = analysis.predict_next_sales(pd.DataFrame(rows), now=datetime(2026, 4, 20)).iloc[0]
    assert out["sales_seen"] == 4 and out["median_gap_days"] == pytest.approx(30)
    assert out["predicted_next"] == pd.Timestamp("2026-05-02") and out["confidence"] == "high"
    assert not out["on_sale_now"]


def test_health_scores_components():
    # players +20% in the last week, flat reviews, 90% positive
    end = pd.Timestamp("2026-03-29")
    players = hourly(1, "2026-03-01", 28, lambda t: 1200 if t > end - timedelta(days=7) else 1000)
    reviews = pd.DataFrame({"appid": 1, "date": pd.date_range("2026-03-01", periods=28, freq="D"),
                            "total_reviews": range(28), "new_reviews": 10.0})
    owners = pd.DataFrame([{"appid": 1, "date": pd.Timestamp("2026-03-28"), "positive": 900, "negative": 100}])
    h = analysis.health_scores(players, reviews, owners).iloc[0]
    assert h["momentum_pct"] == pytest.approx(20, abs=1) and h["momentum_score"] == pytest.approx(75, abs=2)
    assert h["velocity_score"] == pytest.approx(50) and h["quality_score"] == pytest.approx(80)
    assert h["health"] == pytest.approx(0.4 * h["momentum_score"] + 0.2 * 50 + 0.4 * 80)
    # only quality available -> health = quality
    h2 = analysis.health_scores(players.iloc[:0], reviews.iloc[:0], owners).iloc[0]
    assert h2["health"] == pytest.approx(80) and h2["components"] == 1



# --------------------------------------------------------------------------- review sentiment

def test_parse_review_summary():
    from tracker.steam import parse_review_summary
    data = {"success": 1, "query_summary": {"num_reviews": 1, "review_score": 9, "review_score_desc":
            "Overwhelmingly Positive", "total_positive": 950, "total_negative": 50, "total_reviews": 1000}}
    assert parse_review_summary(data) == {"total_positive": 950, "total_negative": 50, "review_score": 9,
                                          "review_score_desc": "Overwhelmingly Positive"}
    assert parse_review_summary({"success": 2}) is None
    assert parse_review_summary({"success": 1, "query_summary": {}}) is None


def test_collect_sentiment_once_per_day(engine, monkeypatch):
    t0 = datetime(2026, 1, 1, 10)
    monkeypatch.setattr(db, "utcnow", lambda: t0)
    games = config.GamesConfig(country="us", appids=[1, 2])
    assert collect.run(engine=engine, client=FakeClient(), games=games).sentiment == 2
    assert collect.run(engine=engine, client=FakeClient(), games=games).sentiment == 0
    s = analysis.load_sentiment(engine, [1])
    assert s.iloc[0]["positive_pct"] == pytest.approx(901 / 1001 * 100)


def _sentiment_rows(appid, start, negatives, positive=200):
    rows, pos, neg = [], 10_000, 1_000
    for i, n in enumerate(negatives):
        pos, neg = pos + positive, neg + n
        rows.append({"appid": appid, "date": (start + timedelta(days=i)).date(), "total_positive": pos,
                     "total_negative": neg, "review_score": 8, "review_score_desc": "Very Positive"})
    return rows


def test_review_bomb_detection_and_alert(engine):
    _seed_game(engine)
    start = datetime(2026, 3, 1)
    db.insert_ignore(engine, db.review_score_snapshots, _sentiment_rows(1, start, [20] * 10 + [400]))
    _event(engine, 1, start + timedelta(days=9, hours=18), "patch", "Patch 2.0", "p")
    sent = analysis.load_sentiment(engine, [1])
    bombs = analysis.review_bombs(sent, analysis.load_events(engine, [1]))
    assert len(bombs) == 1 and bombs.iloc[0]["new_negative"] == 400
    assert bombs.iloc[0]["related_event"] == "patch: Patch 2.0"
    weekly = analysis.weekly_sentiment(sent)
    assert weekly["reviews"].sum() == pytest.approx(9 * 220 + 600)   # first day has no previous day to diff

    cfg = AlertsConfig(sales=False, patches=False, spikes=False, daily_summary=False)
    now = start + timedelta(days=10, hours=12)
    send = FakeSender()
    alerts_mod.process(engine, cfg, [1], send, now=now)
    assert send.titles == ["Review bomb: Game 1"]
    alerts_mod.process(engine, cfg, [1], send, now=now + timedelta(hours=1))   # once per day
    assert len(send.calls) == 1


@pytest.mark.parametrize("neg,pos,hist,expected", [
    (400, 200, [20] * 10, True),
    (400, 500, [20] * 10, False),    # more positive than negative that day
    (25, 0, [1] * 10, False),        # under the minimum
    (100, 10, [50] * 10, False),     # only 2x the usual
])
def test_is_review_bomb(neg, pos, hist, expected):
    assert alerts_mod.is_review_bomb(neg, pos, hist, 30) is expected



# --------------------------------------------------------------------------- regional prices

def test_extra_countries_store_prices_without_events(engine, monkeypatch):
    t0 = datetime(2026, 1, 1, 10)
    monkeypatch.setattr(db, "utcnow", lambda: t0)
    client = FakeClient()
    games = config.GamesConfig(country="us", appids=[1], extra_countries=["gb"])
    collect.run(engine=engine, client=client, games=games)
    client.discount = 50
    monkeypatch.setattr(db, "utcnow", lambda: t0 + timedelta(hours=1))
    collect.run(engine=engine, client=client, games=games)
    allp = analysis.load_prices(engine, [1])
    assert sorted(allp["country"].unique()) == ["gb", "us"] and len(allp) == 4
    assert len(analysis.load_prices(engine, [1], country="us")) == 2
    ev = analysis.load_events(engine, [1], kinds=["sale_start"])
    assert len(ev) == 1 and ev.iloc[0]["source_id"].startswith("us:")   # sale counted once


def test_regional_prices_conversion():
    ts = pd.Timestamp("2026-01-01")
    prices = pd.DataFrame([
        {"appid": 1, "ts": ts, "country": "us", "currency": "USD", "final": 20.0, "initial": 20.0,
         "discount_pct": 0, "is_free": False},
        {"appid": 1, "ts": ts, "country": "gb", "currency": "GBP", "final": 12.0, "initial": 12.0,
         "discount_pct": 0, "is_free": False},
        {"appid": 1, "ts": ts, "country": "br", "currency": "BRL", "final": 50.0, "initial": 50.0,
         "discount_pct": 0, "is_free": False},
    ])
    out = analysis.regional_prices(prices, "us", {"GBP": 0.75}, "USD").set_index("country")
    assert out.loc["gb", "converted"] == pytest.approx(16) and out.loc["gb", "vs_main_pct"] == pytest.approx(-20)
    assert pd.isna(out.loc["br", "converted"])          # no rate -> not converted
    assert out.loc["gb", "cheapest"] and not out.loc["us", "cheapest"]


def test_games_toml_extra_countries(tmp_path):
    p = tmp_path / "g.toml"
    p.write_text('country = "us"\nextra_countries = ["GB", "us", "gb", "de"]\n[[games]]\nappid = 1\n')
    assert config.load_games(p).extra_countries == ["gb", "de"]



# --------------------------------------------------------------------------- twitch

class FakeTwitch:
    def game_ids(self, names):
        return {"game 1": "111"}            # Game 2 isn't on Twitch

    def viewers(self, game_id):
        return (5000, 42)


def test_collect_twitch_matches_names(engine, monkeypatch):
    monkeypatch.setattr(db, "utcnow", lambda: datetime(2026, 1, 1, 10))
    games = config.GamesConfig(country="us", appids=[1, 2])
    stats = collect.run(engine=engine, client=FakeClient(), games=games, twitch=FakeTwitch())
    assert stats.twitch == 1 and "twitch:2" in stats.failures
    tw = analysis.load_twitch(engine, [1, 2])
    assert tw.iloc[0]["viewers"] == 5000 and tw.iloc[0]["appid"] == 1


def test_twitch_helpers():
    from tracker.twitch import clean_game_name, sum_streams
    assert clean_game_name("Counter-Strike™  2") == "Counter-Strike 2"
    assert sum_streams([{"data": [{"viewer_count": 10}, {"viewer_count": 5}]}, {"data": []}]) == (15, 2)


def test_twitch_lead_lag_finds_lead():
    import numpy as np
    rng = np.random.default_rng(1)
    ts = pd.date_range("2026-03-01", periods=24 * 10, freq="h")
    shocks = rng.normal(0, 0.05, len(ts)).cumsum()
    level = 1000 * np.exp(shocks)
    players = pd.DataFrame({"appid": 1, "ts": ts, "players": level})
    viewers = pd.DataFrame({"appid": 1, "ts": ts, "viewers": np.roll(level, -3) / 10})  # viewers 3h ahead
    out = analysis.twitch_lead_lag(players, viewers.iloc[:-3]).iloc[0]
    assert out["best_lag_h"] == 3 and out["best_corr"] > 0.9



# --------------------------------------------------------------------------- followers & release dates

def test_parse_member_count():
    from tracker.steam import parse_member_count
    xml = "<memberList><groupID64>1</groupID64><groupDetails><groupName>x</groupName>" \
          "<memberCount>1234567</memberCount><membersInChat>5</membersInChat></groupDetails></memberList>"
    assert parse_member_count(xml) == 1_234_567
    assert parse_member_count("<html>error</html>") is None and parse_member_count(None) is None


def test_collect_followers_daily(engine, monkeypatch):
    monkeypatch.setattr(db, "utcnow", lambda: datetime(2026, 1, 1, 10))
    games = config.GamesConfig(country="us", appids=[1])
    assert collect.run(engine=engine, client=FakeClient(), games=games).followers == 1
    assert collect.run(engine=engine, client=FakeClient(), games=games).followers == 0
    assert analysis.load_followers(engine, [1]).iloc[0]["followers"] == 10_001


@pytest.mark.parametrize("text,status,guess", [
    ("26 Feb, 2016", "released", datetime(2016, 2, 26)),
    ("Feb 26, 2030", "upcoming", datetime(2030, 2, 26)),
    ("Coming soon", "upcoming", None),
    ("To be announced", "upcoming", None),
    ("Q2 2027", "upcoming", datetime(2027, 6, 30)),
    ("March 2027", "upcoming", datetime(2027, 3, 31)),
    ("2026", "upcoming", datetime(2026, 12, 31)),
    ("2025", "released", datetime(2025, 12, 31)),
    ("", "unknown", None),
])
def test_release_status(text, status, guess):
    assert analysis.release_status(text, datetime(2026, 11, 16)) == (status, guess)


def test_follower_summary_growth():
    games_df = pd.DataFrame([{"appid": 1, "name": "G", "release_date": "Coming soon"}])
    f = pd.DataFrame({"appid": 1, "date": pd.date_range("2026-01-01", periods=31, freq="D"),
                      "followers": [1000 + 10 * i for i in range(31)]})
    s = analysis.follower_summary(games_df, f, datetime(2026, 2, 1)).iloc[0]
    assert s["status"] == "upcoming" and s["followers"] == 1300
    assert s["new_7d"] == 70 and s["new_30d"] == 300

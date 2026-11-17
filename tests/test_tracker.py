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
        return {a: price(self.discount) for a in appids}

    def app_details(self, appid, country):
        return {"name": f"Game {appid}", "is_free": False, "developers": ["Dev"], "publishers": ["Pub"],
                "release_date": {"date": "1 Jan, 2024"}, "recommendations": {"total": 5000 + appid}}

    def news(self, appid):
        return [{"gid": "123", "title": "Patch 1.1", "url": "https://x", "date": 1_700_000_000, "tags": []}]

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

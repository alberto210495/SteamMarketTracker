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
                "release_date": {"date": "1 Jan, 2024"}}

    def news(self, appid):
        return [{"gid": "123", "title": "Patch 1.1", "url": "https://x", "date": 1_700_000_000, "tags": []}]

    def steamspy(self, appid):
        return {"appid": appid, "owners": "1,000 .. 2,000", "positive": 10, "negative": 2, "ccu": 5,
                "average_forever": 60, "median_forever": 30}


@pytest.fixture
def engine(tmp_path):
    return db.get_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")


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


def test_copy_database_is_complete_and_rerunnable(tmp_path):
    from tracker import demo
    src = db.get_engine(f"sqlite:///{(tmp_path / 'src.db').as_posix()}")
    dst = db.get_engine(f"sqlite:///{(tmp_path / 'dst.db').as_posix()}")
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

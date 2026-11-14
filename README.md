# Steam Market Tracker

Tracks Steam **player counts, prices and discounts** over time, detects **sales and patches**, and charts how
players respond to them.

```
 GitHub Actions / cron (hourly)          SQLite or PostgreSQL            Streamlit dashboard
 ┌──────────────────────────┐           ┌──────────────────┐           ┌─────────────────────┐
 │ python -m tracker collect│ ────────▶ │ games            │ ────────▶ │ players + sale bands│
 │  • players  (Web API)    │           │ player_snapshots │           │ prices & sales      │
 │  • prices   (storefront) │           │ price_snapshots  │           │ event impact        │
 │  • news     (Web API)    │           │ owner_snapshots  │           │ indexed comparison  │
 │  • owners   (SteamSpy)   │           │ events           │           │ owners & reviews    │
 └──────────────────────────┘           └──────────────────┘           └─────────────────────┘
```

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements-dev.txt

python -m tracker collect          # one pass against the live APIs -> data/steam.db
streamlit run dashboard/app.py     # http://localhost:8501
```

A tracker needs history to be interesting. To explore the dashboard right away:

```bash
python -m tracker demo                     # 90 days of synthetic data -> data/demo.db
streamlit run dashboard/app.py -- --demo
```

Demo games are named `[Demo] …` and live in a separate database, so they never mix with real data.

## Choosing games

Edit [games.toml](games.toml). Find an app ID in the store URL: `store.steampowered.com/app/<appid>/…`.
`country` sets the storefront region (and currency) for prices.

## What gets collected

| Data | Source | Frequency | Notes |
|---|---|---|---|
| Concurrent players | `ISteamUserStats/GetNumberOfCurrentPlayers` | every run (hourly) | Keyed by hour, so re-runs within the hour are no-ops |
| Price / discount | `store.steampowered.com/api/appdetails?filters=price_overview` | every run | Batched: one request covers up to 50 games |
| Sales | derived | every run | `sale_start` / `sale_end` / `sale_change` / `price_change` events, created by comparing each sample with the previous one |
| Patches & announcements | `ISteamNews/GetNewsForApp` (official announcements feed) | every run | `patch` if tagged `patchnotes` or the title looks like patch notes |
| Owners, reviews, playtime | SteamSpy `appdetails` | once per UTC day | Owners is a coarse **range** estimate |
| Name, developer, etc. | storefront `appdetails` | weekly | |

No API key is required. If you set `STEAM_API_KEY` it is sent with Web API calls.

Requests are rate-limited (storefront ~1 req/1.6 s, SteamSpy 1 req/s) and retried with backoff on 429/5xx.

### Discord alerts

Add a repository secret `DISCORD_WEBHOOK_URL` (Discord: Server Settings → Integrations → Webhooks) and each
collection run posts:

| Alert | When |
|---|---|
| Sale | a tracked game goes on sale, the sale ends, the discount changes, or the base price changes |
| Patch | new patch notes appear on Steam |
| Player spike | players are ≥1.5× (and ≥200 above) the median for that hour of day over the last 14 days; at most one per game per 24h. Needs ~5 days of history to start. |
| Daily summary | once a day, first run after 14:00 UTC: each game's 24h peak, change vs. the day before, current price |

Toggle each one and tune thresholds under `[alerts]` in [games.toml](games.toml). The first run with alerts
enabled sends nothing for events already in the database. If Discord is unreachable, the alerts are retried on the
next run.

Check the webhook: **Actions → Collect Steam data → Run workflow**, tick *Send a test message to Discord*.
Locally: `python -m tracker test-alert` with `DISCORD_WEBHOOK_URL` set.

### Annotating events yourself

Some games name their big updates in ways the classifier won't spot (Rust's monthly updates, for example), and
some events happen off Steam (a streamer spike, a trailer). Add them by hand:

```bash
python -m tracker annotate 252490 "Monthly update: Livestock" --at 2026-11-06T19:00
```

## Measuring how players respond

The **Event impact** tab (also `python -m tracker report <appid> --days 7`) compares the **average daily peak**
in the N days before an event with the N days after. The 24-hour buckets are aligned to the event time, so each
one holds a full day/night cycle and time of day doesn't skew the comparison. Rows without enough samples on
both sides (under 60% coverage) are flagged rather than hidden.

This shows correlation, not causation. A sale that starts on a Thursday will overlap a weekend, Steam seasonal
sales lift everything, and patches often ship at the same time as sales.

## Scheduling

### GitHub Actions (default)

[.github/workflows/collect.yml](.github/workflows/collect.yml) runs every hour at :07 and writes to PostgreSQL
(a free [Neon](https://neon.tech) database works well).

Required repository secret: `DATABASE_URL` (`postgresql://user:pass@host/db?sslmode=require`). The run fails
immediately with a clear error if it's missing. Optional secret: `STEAM_API_KEY`.

> GitHub may delay scheduled runs at busy times. It also disables schedules on repos with no activity for 60
> days; the workflow re-enables itself through the API once a day to prevent that.

### Local cron / Task Scheduler

```bash
# crontab -e
7 * * * * cd /path/to/SteamMarketTracker && .venv/bin/python -m tracker collect >> collect.log 2>&1
```

```powershell
# Windows Task Scheduler, hourly
schtasks /Create /SC HOURLY /TN SteamMarketTracker /TR "cmd /c cd /d C:\path\to\SteamMarketTracker && .venv\Scripts\python.exe -m tracker collect >> collect.log 2>&1"
```

## Using PostgreSQL

```bash
set DATABASE_URL=postgresql://user:pass@localhost:5432/steam      # PowerShell: $env:DATABASE_URL="..."
python -m tracker collect
streamlit run dashboard/app.py
```

Tables are created automatically. The same code runs on both databases (SQLAlchemy Core with
`ON CONFLICT DO NOTHING` upserts).

To move existing SQLite data into Postgres (safe to re-run; rows already there are skipped):

```bash
python -m tracker migrate --to "postgresql://user:pass@host/db"
```

## Hosting the dashboard (Streamlit Community Cloud)

1. Sign in at [share.streamlit.io](https://share.streamlit.io) with GitHub → **Create app** → pick this repo,
   branch `main`, main file `dashboard/app.py`.
2. **Advanced settings → Secrets**:
   ```toml
   DATABASE_URL = "postgresql://user:pass@host/db"
   ```
3. Deploy. The app reads the same database the hourly job writes to; queries are cached for 5 minutes.

For local runs you can put the same line in `.streamlit/secrets.toml` (git-ignored) instead of an env var.

## Project layout

```
tracker/
  steam.py      API clients and pure response parsers
  collect.py    one collection pass + sale detection
  db.py         schema and insert/upsert helpers
  analysis.py   loading, resampling, sale intervals, event impact, indexing
  demo.py       synthetic data generator
  __main__.py   CLI: collect | annotate | report | demo
dashboard/app.py  Streamlit app
tests/            pytest (parsers, sale detection, end-to-end collect with a fake client, analysis)
```

Run tests with `pytest -q`.

## Known limits

- Steam reports *current* players only, so there is no backfill. History starts when you start collecting.
  For long-range context, sites like SteamDB and SteamCharts have archives (check their terms before scraping).
- One storefront region per run. To track several regions, run with different `GAMES_FILE`s, each setting its own
  `country`. Prices are stored per country.
- Owner counts from SteamSpy are estimates, and SteamSpy's accuracy has varied since Steam's 2018 privacy changes.

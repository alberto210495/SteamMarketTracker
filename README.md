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

Either use the dashboard's **Manage games** tab (search by name, click *Track* or *Remove*; needs the
`ADMIN_PASSWORD` secret, see [Hosting the dashboard](#hosting-the-dashboard-streamlit-community-cloud)), or edit
[games.toml](games.toml): find an app ID in the store URL `store.steampowered.com/app/<appid>/…`.

The collector tracks the games in `games.toml`, plus games added in the dashboard, minus games removed there.
Removing a game stops collection and hides it, but keeps its history. Changes apply from the next hourly run.
`country` in `games.toml` sets the storefront region (and currency) for prices. `extra_countries` adds regions whose prices are
stored for comparison only (the *Regional prices* table converts them to your currency with ECB rates from
frankfurter.app and marks the cheapest region); sales, alerts and charts use `country`.

## What gets collected

| Data | Source | Frequency | Notes |
|---|---|---|---|
| Concurrent players | `ISteamUserStats/GetNumberOfCurrentPlayers` | every run (hourly) | Keyed by hour, so re-runs within the hour are no-ops |
| Price / discount | `store.steampowered.com/api/appdetails?filters=price_overview` | every run | Batched: one request covers up to 50 games |
| Sales | derived | every run | `sale_start` / `sale_end` / `sale_change` / `price_change` events, created by comparing each sample with the previous one |
| Patches & announcements | `ISteamNews/GetNewsForApp` (official announcements feed) | every run | `patch` if tagged `patchnotes` or the title looks like patch notes |
| Owners, positive/negative reviews, playtime | SteamSpy `appdetails` | once per UTC day | Owners is a coarse **range** estimate |
| Steam review total | storefront `appdetails` (`recommendations.total`) | once per UTC day | New reviews per day = difference between days |
| Positive / negative reviews | `store.steampowered.com/appreviews` (`query_summary`) | once per UTC day | Steam's own all-language totals; daily differences give the weekly sentiment |
| Twitch viewers (optional) | Twitch Helix `streams` | every run | Needs `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET`; sums the top 1,000 live streams |
| Name, developer, etc. | storefront `appdetails` | weekly | |

No API key is required. If you set `STEAM_API_KEY` it is sent with Web API calls.

**Twitch (optional):** create an application at [dev.twitch.tv/console](https://dev.twitch.tv/console) (category
*Analytics Tool*, any OAuth redirect such as `http://localhost`), then add repository secrets `TWITCH_CLIENT_ID` and
`TWITCH_CLIENT_SECRET`. Games are matched to Twitch categories by name; if a name differs, add
`twitch_name = "..."` to that game in `games.toml`. The player charts overlay viewers, and the *Patterns* tab shows
whether viewer changes come before player changes.

Requests are rate-limited (storefront ~1 req/1.6 s, SteamSpy 1 req/s) and retried with backoff on 429/5xx.

### Discord alerts

Add a repository secret `DISCORD_WEBHOOK_URL` (Discord: Server Settings → Integrations → Webhooks) and each
collection run posts:

| Alert | When |
|---|---|
| Sale | a tracked game goes on sale, the sale ends, the discount changes, or the base price changes |
| Patch | new patch notes appear on Steam |
| Player spike | players are ≥1.5× (and ≥200 above) the median for that hour of day over the last 14 days; at most one per game per 24h. Needs ~5 days of history to start. |
| Review bomb | a day's new negative reviews are ≥30, the majority of that day's reviews, and ≥3× the usual daily negatives (14-day median) |
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

**Vs market** (the default) compares that change with the rest of your tracked games over the same days: the
median change of the other games is the *market change*, and *vs market* = (1 + game change) / (1 + market
change) − 1. If everything rose 20% during a Steam-wide sale and your game rose 50%, it did 25% better than the
market. It needs at least two other games with enough data around the event.

**Market-wide sales** are detected from the price data: periods when at least half of the tracked paid games (and at
least 3) are discounted at once, usually a Steam seasonal sale. They're shaded grey on the player charts and
listed on the *Prices & sales* tab. The same tab estimates each game's **next sale** (last sale + the typical gap
between its sales, with a confidence based on how regular the gaps are) and the next Steam seasonal sale, estimated
from Valve's usual schedule. The more games you track, the more reliable both of these become.

**Sale effectiveness** (*Prices & sales* tab) plots each sale's discount against its player lift (the
event-impact change at the sale's start, vs. market when available) and reports *lift per 10% off*. A median by
discount bracket shows where deeper discounts stop paying off.

**How long bumps last** (same tab) follows each event's daily peaks for up to 30 days: peak lift in the first
week, *half-life* (days until half the gain is gone) and days until players are back within 10% of the baseline.

The **Patterns** tab shows each game's weekly rhythm: a weekday × hour heatmap of average players (in your time
zone), the busiest and quietest hour, and how much busier weekends are.

**Review sentiment** (*Owners & reviews*) charts the % positive of the reviews written each week, so a bad patch
shows up long before it moves the all-time score. Review bombs are marked and listed with any patch or announcement
in the 3 days before.

**Health score** (0–100, first column of the overview table; breakdown on *Owners & reviews*): 40% player momentum
(average daily peak, last 7 days vs. the 21 before), 20% review velocity (new reviews per day, same windows) and 40%
review quality (% positive). Trends score 50 when flat and 0/100 at ±40%; parts without enough data are left out.

This is still correlation, not causation. Patches often ship at the same time as sales, and a single game's news
(a streamer, a trailer) won't show up in the market baseline.

All times in the dashboard are shown in your browser's time zone by default (switchable in the sidebar); the
database stores UTC.

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
   DATABASE_URL = "postgresql://dashboard_ro:pass@host/db"   # read-only role
   ADMIN_PASSWORD = "something long"                          # optional: enables the Manage games tab
   ```
3. Deploy. The app reads the same database the hourly job writes to; queries are cached for 5 minutes.

The dashboard should connect with a read-only role. The only write it ever makes is to the `tracked_games`
table, from the Manage games tab, so that role needs `GRANT SELECT, INSERT, UPDATE ON tracked_games`. The
dashboard never creates tables; the collector owns the schema.

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
- Sales are detected in the main `country` only; `extra_countries` are price snapshots for comparison.
- Owner counts from SteamSpy are estimates, and SteamSpy's accuracy has varied since Steam's 2018 privacy changes.

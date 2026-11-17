"""Runtime configuration: env vars + games.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "steam.db"


def database_url() -> str:
    """SQLite file by default; set DATABASE_URL for PostgreSQL.

    Example: postgresql+psycopg://user:pass@host:5432/steam
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        # Many hosts hand out postgres:// URLs; SQLAlchemy needs a driver name.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url
    DEFAULT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"


def steam_api_key() -> str | None:
    """Optional. The endpoints used here work without a key, but a key is sent if present."""
    return os.environ.get("STEAM_API_KEY") or None


@dataclass
class GamesConfig:
    country: str = "us"
    appids: list[int] = field(default_factory=list)
    extra_countries: list[str] = field(default_factory=list)   # price snapshots only, no sale events
    twitch_names: dict[int, str] = field(default_factory=dict)  # appid -> Twitch category name override
    targets: dict[int, float] = field(default_factory=dict)     # appid -> alert at or below this price
    alerts: "AlertsConfig" = field(default_factory=lambda: AlertsConfig())


@dataclass
class AlertsConfig:
    """[alerts] in games.toml. Alerts only go out when DISCORD_WEBHOOK_URL is set."""
    sales: bool = True              # sale start / end / discount change / base price change
    patches: bool = True            # new patch notes on Steam
    spikes: bool = True             # players well above the usual level for that hour
    spike_ratio: float = 1.5        # ... at least this many times the usual level
    spike_min_increase: int = 200   # ... and at least this many extra players
    daily_summary: bool = True
    daily_summary_hour_utc: int = 14
    review_bombs: bool = True       # a day with far more negative reviews than usual
    review_bomb_min_negative: int = 30
    price_targets: bool = True      # a game drops to or below its `target_price` in [[games]]
    all_time_lows: bool = True      # a discounted game hits its lowest price since tracking began
    atl_min_history_days: int = 30  # ...once there's at least this much price history


def twitch_credentials() -> tuple[str, str] | None:
    cid = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def discord_webhook_url() -> str | None:
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip() or None


def load_games(path: Path | None = None) -> GamesConfig:
    path = path or Path(os.environ.get("GAMES_FILE", ROOT / "games.toml"))
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    appids = [int(g["appid"]) for g in raw.get("games", [])]
    # de-duplicate, keep order
    seen: set[int] = set()
    appids = [a for a in appids if not (a in seen or seen.add(a))]
    known = AlertsConfig.__dataclass_fields__
    alerts = AlertsConfig(**{k: v for k, v in (raw.get("alerts") or {}).items() if k in known})
    country = str(raw.get("country", "us")).lower()
    extra = [str(c).lower() for c in raw.get("extra_countries") or []]
    extra = [c for i, c in enumerate(extra) if c != country and c not in extra[:i]]
    twitch = {int(g["appid"]): str(g["twitch_name"]) for g in raw.get("games", []) if g.get("twitch_name")}
    targets = {int(g["appid"]): float(g["target_price"]) for g in raw.get("games", [])
               if g.get("target_price") is not None}
    return GamesConfig(country=country, appids=appids, alerts=alerts, extra_countries=extra, twitch_names=twitch,
                       targets=targets)

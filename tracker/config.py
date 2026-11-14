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


def load_games(path: Path | None = None) -> GamesConfig:
    path = path or Path(os.environ.get("GAMES_FILE", ROOT / "games.toml"))
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    appids = [int(g["appid"]) for g in raw.get("games", [])]
    # de-duplicate, keep order
    seen: set[int] = set()
    appids = [a for a in appids if not (a in seen or seen.add(a))]
    return GamesConfig(country=str(raw.get("country", "us")).lower(), appids=appids)

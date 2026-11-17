"""Twitch Helix client: live viewers per game (app access token, no user login).

Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (create an application at
dev.twitch.tv/console). Without them the collector skips this step.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"
MAX_PAGES = 10   # streams come sorted by viewers, so 1,000 streams covers nearly all viewers


def clean_game_name(name: str) -> str:
    """Steam names carry ™/® and odd spacing that Twitch's catalogue doesn't."""
    return re.sub(r"\s+", " ", re.sub(r"[™®©]", "", name or "")).strip()


def sum_streams(pages: list[dict[str, Any]]) -> tuple[int, int]:
    """(total viewers, streams) over pages of /helix/streams responses."""
    viewers = streams = 0
    for page in pages:
        for s in page.get("data") or []:
            viewers += int(s.get("viewer_count") or 0)
            streams += 1
    return viewers, streams


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str, timeout: float = 15.0):
        self.client_id, self.client_secret, self.timeout = client_id, client_secret, timeout
        self.session = requests.Session()
        self._token: str | None = None

    def _auth(self) -> str:
        if self._token is None:
            resp = self.session.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret,
                                                      "grant_type": "client_credentials"}, timeout=self.timeout)
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
        return self._token

    def _get(self, path: str, params: list[tuple[str, Any]]) -> dict[str, Any] | None:
        for attempt in range(3):
            try:
                resp = self.session.get(f"{HELIX}/{path}", params=params, timeout=self.timeout, headers={
                    "Client-Id": self.client_id, "Authorization": f"Bearer {self._auth()}"})
            except requests.RequestException as exc:
                log.warning("Twitch %s failed (%s)", path, type(exc).__name__)
                time.sleep(2)
                continue
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and attempt == 0:
                self._token = None   # expired token: fetch a new one once
                continue
            if resp.status_code == 429:
                time.sleep(max(1.0, float(resp.headers.get("Ratelimit-Reset", time.time() + 2)) - time.time()))
                continue
            log.warning("Twitch %s -> HTTP %d", path, resp.status_code)
            return None
        return None

    def game_ids(self, names: list[str]) -> dict[str, str]:
        """{cleaned name (lowercase): twitch game id} for the names Twitch knows."""
        out: dict[str, str] = {}
        cleaned = [clean_game_name(n) for n in names if n]
        for i in range(0, len(cleaned), 100):
            data = self._get("games", [("name", n) for n in cleaned[i:i + 100]])
            for g in (data or {}).get("data") or []:
                out[clean_game_name(g["name"]).lower()] = str(g["id"])
        return out

    def viewers(self, game_id: str) -> tuple[int, int] | None:
        pages, cursor = [], None
        for _ in range(MAX_PAGES):
            params: list[tuple[str, Any]] = [("game_id", game_id), ("first", 100)]
            if cursor:
                params.append(("after", cursor))
            data = self._get("streams", params)
            if data is None:
                return None if not pages else sum_streams(pages)
            pages.append(data)
            cursor = (data.get("pagination") or {}).get("cursor")
            if not cursor or len(data.get("data") or []) < 100:
                break
        return sum_streams(pages)

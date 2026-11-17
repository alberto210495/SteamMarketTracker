"""Thin clients for the Steam Web API, Steam storefront, and SteamSpy.

Each client is rate-limited and retries transient failures (429 / 5xx / network)
with exponential backoff. Parsers are pure functions so they can be unit-tested.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)

USER_AGENT = "steam-market-tracker/0.1 (+https://github.com/)"

WEB_API = "https://api.steampowered.com"
STORE_API = "https://store.steampowered.com/api"
STEAMSPY_API = "https://steamspy.com/api.php"


class HttpClient:
    def __init__(self, min_interval: float, timeout: float = 20.0, retries: int = 4):
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any | None:
        resp = self._get(url, params)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            log.warning("Non-JSON response from %s", url)
            return None

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str | None:
        resp = self._get(url, params)
        return resp.text if resp is not None else None

    def _get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response | None:
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("GET %s failed (%s), attempt %d/%d", url, exc, attempt, self.retries)
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, float(retry_after))
                    log.warning("GET %s -> %d, attempt %d/%d", url, resp.status_code, attempt, self.retries)
                else:
                    log.warning("GET %s -> %d (not retrying)", url, resp.status_code)
                    return None
            if attempt < self.retries:
                time.sleep(delay)
                delay *= 2
        return None


# --------------------------------------------------------------------------- parsers

@dataclass
class PriceInfo:
    is_free: bool
    currency: str | None
    initial_cents: int | None
    final_cents: int | None
    discount_pct: int


def parse_price_entry(entry: dict[str, Any] | None) -> PriceInfo | None:
    """Parse one appid's entry from appdetails?filters=price_overview.

    Quirk: free (or unreleased / unpriced) games come back as
    {"success": true, "data": []} - an empty list instead of a dict.
    """
    if not entry or not entry.get("success"):
        return None
    data = entry.get("data")
    if not isinstance(data, dict) or "price_overview" not in data:
        return PriceInfo(is_free=True, currency=None, initial_cents=0, final_cents=0, discount_pct=0)
    po = data["price_overview"]
    return PriceInfo(
        is_free=False,
        currency=po.get("currency"),
        initial_cents=int(po.get("initial", 0)),
        final_cents=int(po.get("final", 0)),
        discount_pct=int(po.get("discount_percent", 0)),
    )


def parse_owner_range(owners: str | None) -> tuple[int | None, int | None]:
    """SteamSpy owners look like '20,000,000 .. 50,000,000'."""
    if not owners:
        return None, None
    nums = [int(n.replace(",", "")) for n in re.findall(r"[\d,]+", owners) if n.replace(",", "")]
    if len(nums) >= 2:
        return nums[0], nums[1]
    if len(nums) == 1:
        return nums[0], nums[0]
    return None, None


def parse_review_total(details: dict[str, Any] | None) -> int | None:
    """Steam's running review count from appdetails: {"recommendations": {"total": N}}."""
    total = ((details or {}).get("recommendations") or {}).get("total")
    try:
        return int(total) if total is not None else None
    except (TypeError, ValueError):
        return None


def parse_review_summary(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """appreviews?json=1 -> {"success": 1, "query_summary": {"total_positive": .., "total_negative": ..,
    "review_score": 9, "review_score_desc": "Overwhelmingly Positive", ...}}."""
    if not data or not data.get("success"):
        return None
    qs = data.get("query_summary") or {}
    try:
        pos, neg = int(qs["total_positive"]), int(qs["total_negative"])
    except (KeyError, TypeError, ValueError):
        return None
    score = qs.get("review_score")
    return {"total_positive": pos, "total_negative": neg,
            "review_score": int(score) if isinstance(score, (int, float)) else None,
            "review_score_desc": (qs.get("review_score_desc") or None) and str(qs["review_score_desc"])[:64]}


_MEMBER_COUNT_RE = re.compile(r"<memberCount>\s*(\d+)\s*</memberCount>")


def parse_member_count(xml: str | None) -> int | None:
    """Community hub followers from steamcommunity.com/games/<appid>/memberslistxml/?xml=1."""
    m = _MEMBER_COUNT_RE.search(xml or "")
    return int(m.group(1)) if m else None


_PATCH_RE = re.compile(
    r"\b(patch|hotfix|hot-fix|update|changelog|release notes|v\d+(\.\d+)+|version \d)",
    re.IGNORECASE,
)


_NOT_PATCH_RE = re.compile(r"\b(community update|state of the game)\b", re.IGNORECASE)


def classify_news(item: dict[str, Any]) -> str:
    """'patch' if the post looks like patch notes, otherwise 'announcement'."""
    tags = [t.lower() for t in item.get("tags") or []]
    if "patchnotes" in tags:
        return "patch"
    title = item.get("title") or ""
    if _NOT_PATCH_RE.search(title):
        return "announcement"
    if _PATCH_RE.search(title):
        return "patch"
    return "announcement"


# --------------------------------------------------------------------------- client

class SteamClient:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key
        self.web = HttpClient(min_interval=0.25)
        # Storefront tolerates roughly 200 requests / 5 minutes.
        self.store = HttpClient(min_interval=1.6)
        # SteamSpy asks for at most 1 request per second.
        self.spy = HttpClient(min_interval=1.1)
        self.community = HttpClient(min_interval=1.6)

    def _web_params(self, **params: Any) -> dict[str, Any]:
        if self.api_key:
            params["key"] = self.api_key
        return params

    def current_players(self, appid: int) -> int | None:
        data = self.web.get_json(
            f"{WEB_API}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
            self._web_params(appid=appid),
        )
        resp = (data or {}).get("response") or {}
        if resp.get("result") == 1 and "player_count" in resp:
            return int(resp["player_count"])
        return None

    def prices(self, appids: list[int], country: str, chunk: int = 50) -> dict[int, PriceInfo]:
        """Batch price lookup. Multiple appids are only allowed with filters=price_overview."""
        out: dict[int, PriceInfo] = {}
        for i in range(0, len(appids), chunk):
            batch = appids[i : i + chunk]
            data = self.store.get_json(
                f"{STORE_API}/appdetails",
                {"appids": ",".join(map(str, batch)), "cc": country, "filters": "price_overview"},
            )
            if not data:
                continue
            for appid in batch:
                info = parse_price_entry(data.get(str(appid)))
                if info is not None:
                    out[appid] = info
        return out

    def app_details(self, appid: int, country: str) -> dict[str, Any] | None:
        data = self.store.get_json(f"{STORE_API}/appdetails", {"appids": appid, "cc": country, "l": "english"})
        entry = (data or {}).get(str(appid)) or {}
        if not entry.get("success"):
            return None
        return entry.get("data")

    def review_summary(self, appid: int) -> dict[str, Any] | None:
        """All-language, all-purchase review totals. One review is requested because the
        summary only comes with the first page."""
        data = self.store.get_json(f"https://store.steampowered.com/appreviews/{appid}",
                                   {"json": 1, "language": "all", "purchase_type": "all", "filter": "recent",
                                    "num_per_page": 1})
        return parse_review_summary(data)

    def followers(self, appid: int) -> int | None:
        """Members of the game's community hub group: what SteamDB calls followers,
        the closest public stand-in for wishlists."""
        return parse_member_count(self.community.get_text(
            f"https://steamcommunity.com/games/{appid}/memberslistxml/", {"xml": 1}))

    def news(self, appid: int, count: int = 20) -> list[dict[str, Any]]:
        data = self.web.get_json(
            f"{WEB_API}/ISteamNews/GetNewsForApp/v2/",
            {"appid": appid, "count": count, "maxlength": 400, "feeds": "steam_community_announcements"},
        )
        return ((data or {}).get("appnews") or {}).get("newsitems") or []

    def steamspy(self, appid: int) -> dict[str, Any] | None:
        data = self.spy.get_json(STEAMSPY_API, {"request": "appdetails", "appid": appid})
        if not data or not data.get("appid"):
            return None
        return data


def unix_to_utc(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).replace(tzinfo=None)

import asyncio
import logging
import re

import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

# Steam blocks bare aiohttp requests; provide a browser-like User-Agent.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Cookies Steam expects for store pages (age gate, cookie consent, language).
_STORE_COOKIES = {
    "birthtime": "0",
    "wants_mature_content": "1",
    "lastagecheckage": "1-0-2000",
    "Steam_Language": "english",
}


class SteamAPI:
    """Client for the Steam Web API."""

    BASE_URL = "https://api.steampowered.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=_HEADERS)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_owned_games(self, steam_id: str) -> list[dict]:
        """Fetch all owned games for a Steam user.

        Returns a list of dicts with keys: app_id, name, playtime_minutes.
        Requires the user's Steam profile game details to be public.
        """
        session = await self._get_session()
        url = f"{self.BASE_URL}/IPlayerService/GetOwnedGames/v1/"
        params = {
            "key": self.api_key,
            "steamid": steam_id,
            "include_appinfo": "true",
            "include_played_free_games": "true",
            "format": "json",
        }

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise Exception(f"Steam API error: HTTP {resp.status}")
            data = await resp.json()

        response = data.get("response", {})
        raw_games = response.get("games", [])

        return [
            {
                "app_id": game["appid"],
                "name": game.get("name", f"Unknown ({game['appid']})"),
                "playtime_minutes": game.get("playtime_forever", 0),
            }
            for game in raw_games
        ]

    async def get_player_summary(self, steam_id: str) -> Optional[dict]:
        """Fetch a player's profile summary (display name, avatar, etc.)."""
        session = await self._get_session()
        url = f"{self.BASE_URL}/ISteamUser/GetPlayerSummaries/v2/"
        params = {
            "key": self.api_key,
            "steamids": steam_id,
            "format": "json",
        }

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        players = data.get("response", {}).get("players", [])
        if not players:
            return None

        player = players[0]
        return {
            "steam_id": player.get("steamid"),
            "display_name": player.get("personaname", "Unknown"),
            "avatar_url": player.get("avatarfull", ""),
            "profile_url": player.get("profileurl", ""),
        }

    async def resolve_vanity_url(self, vanity_name: str) -> Optional[str]:
        """Resolve a Steam vanity URL name to a SteamID64.

        For example, if someone's profile is steamcommunity.com/id/gaben,
        passing 'gaben' returns their SteamID64.
        """
        session = await self._get_session()
        url = f"{self.BASE_URL}/ISteamUser/ResolveVanityURL/v1/"
        params = {
            "key": self.api_key,
            "vanityurl": vanity_name,
            "format": "json",
        }

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        response = data.get("response", {})
        if response.get("success") == 1:
            return response.get("steamid")
        return None

    async def search_free_games(self, num_pages: int = 4, page_size: int = 50) -> list[dict]:
        """Scrape Steam store search for free games.

        Uses the store search with maxprice=free and category1=998 (Games).
        Parses both app IDs and names from the HTML, avoiding the
        rate-limited appdetails endpoint entirely.
        Returns a list of dicts with keys: app_id, name.
        """
        session = await self._get_session()
        # app_id -> name, deduplicates across pages
        found_games: dict[int, str] = {}

        for page in range(num_pages):
            params = {
                "category1": "998",
                "maxprice": "free",
                "ndl": "1",
                "start": str(page * page_size),
                "count": str(page_size),
            }
            try:
                async with session.get(
                    "https://store.steampowered.com/search/",
                    params=params,
                    cookies=_STORE_COOKIES,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        body_preview = await resp.text()
                        logger.warning(
                            "Steam search page %d returned HTTP %d\n"
                            "  URL: %s\n"
                            "  Response headers: %s\n"
                            "  Body (first 500 chars): %s",
                            page,
                            resp.status,
                            resp.url,
                            dict(resp.headers),
                            body_preview[:500],
                        )
                        continue
                    html = await resp.text()
                # Each search result has data-ds-appid="<id>" and <span class="title">Name</span>
                matches = re.findall(
                    r'data-ds-appid="(\d+)".*?<span class="title">([^<]+)</span>',
                    html,
                    re.DOTALL,
                )
                for app_id_str, name in matches:
                    found_games[int(app_id_str)] = name.strip()
                logger.info("Steam search page %d: found %d games", page, len(matches))
            except Exception as e:
                logger.warning("Steam search page %d failed: %s", page, e)
                continue

        logger.info("Steam search total: %d unique free games", len(found_games))
        return [{"app_id": aid, "name": name} for aid, name in found_games.items()]

    async def confirm_free_games(self, app_ids: list[int]) -> list[dict]:
        """Confirm which app IDs are free games via appdetails.

        Returns a list of dicts with keys: app_id, name for confirmed free games.
        """
        confirmed: list[dict] = []
        session = await self._get_session()

        for i in range(0, len(app_ids), 5):
            batch = app_ids[i : i + 5]
            results = await asyncio.gather(
                *(self._get_appdetails(session, aid) for aid in batch)
            )
            for result in results:
                if result is not None:
                    confirmed.append(result)
            if i + 5 < len(app_ids):
                await asyncio.sleep(0.3)

        return confirmed

    async def _get_appdetails(
        self, session: aiohttp.ClientSession, app_id: int
    ) -> Optional[dict]:
        """Fetch appdetails for a single app. Returns dict if free game, else None."""
        url = "https://store.steampowered.com/api/appdetails"
        params = {"appids": str(app_id), "l": "english", "cc": "US"}
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    return None
                # content_type=None: Steam sometimes returns text/html content-type
                data = await resp.json(content_type=None)
            app_data = data.get(str(app_id), {})
            if not app_data.get("success"):
                return None
            details = app_data.get("data", {})
            if details.get("type") != "game":
                return None
            if not details.get("is_free"):
                return None
            return {"app_id": app_id, "name": details.get("name", f"App {app_id}")}
        except Exception as e:
            logger.debug("appdetails failed for %s: %s", app_id, e)
            return None

    async def check_free_apps(self, app_ids: list[int]) -> set[int]:
        """Check which Steam app IDs are free-to-play using appdetails pricing.

        Checks sequentially in small batches to avoid Steam rate limiting.
        Returns a set of app IDs that are free.
        """
        confirmed = await self.confirm_free_games(app_ids)
        return {g["app_id"] for g in confirmed}

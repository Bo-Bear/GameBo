import asyncio

import aiohttp
from typing import Optional


class SteamAPI:
    """Client for the Steam Web API."""

    BASE_URL = "https://api.steampowered.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
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

    async def check_free_apps(self, app_ids: list[int]) -> set[int]:
        """Check which Steam app IDs are free-to-play using appdetails pricing.

        Returns a set of app IDs that are free.
        """
        free_ids: set[int] = set()
        session = await self._get_session()
        semaphore = asyncio.Semaphore(5)

        async def _check_one(app_id: int):
            async with semaphore:
                url = "https://store.steampowered.com/api/appdetails"
                params = {"appids": str(app_id), "filters": "basic"}
                try:
                    async with session.get(url, params=params) as resp:
                        if resp.status != 200:
                            return
                        data = await resp.json()
                    app_data = data.get(str(app_id), {})
                    if app_data.get("success") and app_data.get("data", {}).get("is_free"):
                        free_ids.add(app_id)
                except Exception:
                    pass

        await asyncio.gather(*(_check_one(aid) for aid in app_ids))
        return free_ids

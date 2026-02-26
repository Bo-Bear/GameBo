import asyncio
import time
import aiohttp
from typing import Optional


class IGDBClient:
    """Client for the IGDB API (via Twitch OAuth).

    Used to look up max co-op player counts for games by their Steam app ID.
    """

    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    IGDB_URL = "https://api.igdb.com/v4"
    MAX_BATCH_SIZE = 50
    REQUESTS_PER_SECOND = 4

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_request_time: float = 0
        self._request_interval: float = 1.0 / self.REQUESTS_PER_SECOND

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _ensure_token(self):
        """Get or refresh the Twitch OAuth access token."""
        if self._access_token and time.time() < self._token_expires_at - 60:
            return

        session = await self._get_session()
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }

        async with session.post(self.TOKEN_URL, params=params) as resp:
            if resp.status != 200:
                raise Exception(f"Twitch OAuth error: HTTP {resp.status}")
            data = await resp.json()

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600)

    async def _rate_limit(self):
        """Enforce rate limiting to stay under IGDB's limit."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._request_interval:
            await asyncio.sleep(self._request_interval - elapsed)
        self._last_request_time = time.time()

    async def _query(self, endpoint: str, body: str) -> list[dict]:
        """Send a query to an IGDB endpoint."""
        await self._ensure_token()
        await self._rate_limit()

        session = await self._get_session()
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {self._access_token}",
        }
        url = f"{self.IGDB_URL}/{endpoint}"

        async with session.post(url, headers=headers, data=body) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"IGDB API error: HTTP {resp.status} - {text}")
            return await resp.json()

    async def get_max_players_batch(self, steam_app_ids: list[int]) -> dict[int, int]:
        """Look up max co-op player counts for a batch of Steam app IDs.

        Returns a dict mapping steam_app_id -> max_players.
        Only includes games where player count data was found.
        """
        results: dict[int, int] = {}

        for i in range(0, len(steam_app_ids), self.MAX_BATCH_SIZE):
            batch = steam_app_ids[i : i + self.MAX_BATCH_SIZE]
            batch_results = await self._lookup_batch(batch)
            results.update(batch_results)

        return results

    async def _lookup_batch(self, steam_app_ids: list[int]) -> dict[int, int]:
        """Look up a single batch of Steam app IDs."""
        if not steam_app_ids:
            return {}

        # Step 1: Find IGDB game IDs from Steam app IDs
        uid_list = ",".join(f'"{aid}"' for aid in steam_app_ids)
        query = f"fields uid, game; where uid = ({uid_list}) & category = 1; limit {len(steam_app_ids)};"
        external_games = await self._query("external_games", query)

        if not external_games:
            return {}

        # Build mapping: igdb_game_id -> steam_app_id
        igdb_to_steam: dict[int, int] = {}
        for eg in external_games:
            game_id = eg.get("game")
            uid = eg.get("uid")
            if game_id and uid:
                try:
                    igdb_to_steam[game_id] = int(uid)
                except (ValueError, TypeError):
                    continue

        if not igdb_to_steam:
            return {}

        # Step 2: Get multiplayer modes for those games
        game_ids = ",".join(str(gid) for gid in igdb_to_steam.keys())
        query = (
            f"fields game, onlinecoopmax, onlinemax, offlinecoopmax, offlinemax; "
            f"where game = ({game_ids}); limit 500;"
        )
        multiplayer_modes = await self._query("multiplayer_modes", query)

        # Step 3: Extract max player counts
        results: dict[int, int] = {}
        for mode in multiplayer_modes:
            game_id = mode.get("game")
            if game_id not in igdb_to_steam:
                continue

            steam_id = igdb_to_steam[game_id]

            # Priority: onlinecoopmax > onlinemax > offlinecoopmax > offlinemax
            max_players = (
                mode.get("onlinecoopmax")
                or mode.get("onlinemax")
                or mode.get("offlinecoopmax")
                or mode.get("offlinemax")
                or 0
            )

            if max_players > 0:
                # Keep the highest value if multiple modes exist
                results[steam_id] = max(results.get(steam_id, 0), max_players)

        return results

    async def find_multiplayer_games(self, min_players: int, limit: int = 100) -> list[dict]:
        """Find popular games with multiplayer support for at least min_players.

        Uses separate queries (not field expansion) for reliability.
        Returns a list of dicts with keys: name, steam_app_id, max_players.
        """
        # Step 1: Get popular games that have multiplayer modes
        games_query = (
            "fields id, name; "
            "where multiplayer_modes != null & category = 0; "
            "sort total_rating_count desc; "
            "limit 500;"
        )
        games = await self._query("games", games_query)
        if not games:
            return []

        game_ids_str = ",".join(str(g["id"]) for g in games)
        game_names = {g["id"]: g.get("name", "Unknown") for g in games}

        # Step 2: Get multiplayer modes for those games (separate query)
        modes_query = (
            f"fields game, onlinecoopmax, onlinemax, offlinecoopmax, offlinemax; "
            f"where game = ({game_ids_str}); limit 500;"
        )
        modes = await self._query("multiplayer_modes", modes_query)

        # Build game_id -> max_players mapping
        game_player_counts: dict[int, int] = {}
        for mode in modes:
            game_id = mode.get("game")
            if not game_id:
                continue
            mp = (
                mode.get("onlinecoopmax")
                or mode.get("onlinemax")
                or mode.get("offlinecoopmax")
                or mode.get("offlinemax")
                or 0
            )
            if mp >= min_players:
                game_player_counts[game_id] = max(game_player_counts.get(game_id, 0), mp)

        if not game_player_counts:
            return []

        # Step 3: Get Steam app IDs for matching games (separate query)
        matching_ids_str = ",".join(str(gid) for gid in game_player_counts)
        ext_query = (
            f"fields uid, game; "
            f"where game = ({matching_ids_str}) & category = 1; "
            f"limit 500;"
        )
        external_games = await self._query("external_games", ext_query)

        # Step 4: Combine everything
        results = []
        for eg in external_games:
            game_id = eg.get("game")
            uid = eg.get("uid")
            if not game_id or not uid or game_id not in game_player_counts:
                continue
            try:
                steam_app_id = int(uid)
            except (ValueError, TypeError):
                continue
            results.append({
                "name": game_names.get(game_id, "Unknown"),
                "steam_app_id": steam_app_id,
                "max_players": game_player_counts[game_id],
            })

        return results[:limit]

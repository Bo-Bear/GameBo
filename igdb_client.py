import asyncio
import logging
import time
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)


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
        query = f"fields uid, game; where uid = ({uid_list}) & category = 1; limit 500;"
        try:
            external_games = await self._query("external_games", query)
        except Exception as e:
            logger.warning("[igdb] external_games query failed: %s", e)
            return {}

        logger.info("[igdb] external_games: %d results for %d app IDs", len(external_games), len(steam_app_ids))

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

        logger.info("[igdb] mapped %d IGDB game IDs", len(igdb_to_steam))
        if not igdb_to_steam:
            return {}

        # Step 2: Get multiplayer modes for those games
        game_ids = ",".join(str(gid) for gid in igdb_to_steam.keys())
        query = (
            f"fields game, onlinecoopmax, onlinemax, offlinecoopmax, offlinemax; "
            f"where game = ({game_ids}); limit 500;"
        )
        try:
            multiplayer_modes = await self._query("multiplayer_modes", query)
        except Exception as e:
            logger.warning("[igdb] multiplayer_modes query failed: %s", e)
            return {}

        logger.info("[igdb] multiplayer_modes: %d results", len(multiplayer_modes))

        # Step 3: Extract max player counts
        results: dict[int, int] = {}
        for mode in multiplayer_modes:
            game_id = mode.get("game")
            if game_id not in igdb_to_steam:
                continue

            steam_id = igdb_to_steam[game_id]

            vals = [
                mode.get("onlinecoopmax"),
                mode.get("onlinemax"),
                mode.get("offlinecoopmax"),
                mode.get("offlinemax"),
            ]
            vals = [v for v in vals if isinstance(v, int) and v > 0]
            if vals:
                results[steam_id] = max(results.get(steam_id, 0), max(vals))

        logger.info("[igdb] batch result: %d games with player data", len(results))
        return results

    async def map_steam_appids(self, steam_app_ids: list[int]) -> dict[int, int]:
        """Map Steam app IDs to IGDB game IDs via external_games endpoint.

        Uses category = 1 (Steam) in IGDB's external_games table.
        Returns a dict mapping steam_app_id -> igdb_game_id.
        """
        result: dict[int, int] = {}

        for i in range(0, len(steam_app_ids), self.MAX_BATCH_SIZE):
            batch = steam_app_ids[i : i + self.MAX_BATCH_SIZE]
            uid_list = ",".join(f'"{aid}"' for aid in batch)
            query = (
                f"fields uid, game; "
                f"where uid = ({uid_list}) & category = 1; "
                f"limit {len(batch)};"
            )
            rows = await self._query("external_games", query)
            for row in rows:
                try:
                    result[int(row["uid"])] = int(row["game"])
                except (ValueError, TypeError, KeyError):
                    continue

        return result

    async def get_multiplayer_max_players(self, igdb_game_ids: list[int]) -> dict[int, int]:
        """Get max supported player counts from IGDB multiplayer_modes.

        Returns a dict mapping igdb_game_id -> max_players.
        Computes max across onlinemax, onlinecoopmax, offlinemax, offlinecoopmax.
        """
        result: dict[int, int] = {}

        for i in range(0, len(igdb_game_ids), self.MAX_BATCH_SIZE):
            batch = igdb_game_ids[i : i + self.MAX_BATCH_SIZE]
            ids_str = ",".join(str(gid) for gid in batch)
            query = (
                f"fields game, onlinemax, onlinecoopmax, offlinemax, offlinecoopmax; "
                f"where game = ({ids_str}); limit 500;"
            )
            rows = await self._query("multiplayer_modes", query)
            for row in rows:
                gid = row.get("game")
                if gid is None:
                    continue
                vals = [
                    row.get("onlinemax"),
                    row.get("onlinecoopmax"),
                    row.get("offlinemax"),
                    row.get("offlinecoopmax"),
                ]
                vals = [v for v in vals if isinstance(v, int) and v > 0]
                if vals:
                    result[gid] = max(result.get(gid, 0), max(vals))

        return result

    async def find_multiplayer_games(self, min_players: int, limit: int = 100) -> list[dict]:
        """Find popular multiplayer games with Steam app IDs using separate queries.

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
        logger.info("[igdb] Step 1 - games with multiplayer: %d", len(games))
        if not games:
            return []

        game_names = {g["id"]: g.get("name", "Unknown") for g in games}
        igdb_ids = list(game_names.keys())

        # Step 2: Get multiplayer modes and filter by min_players
        igdb_to_max = await self.get_multiplayer_max_players(igdb_ids)
        # Filter to games meeting min_players threshold
        matching = {gid: mp for gid, mp in igdb_to_max.items() if mp >= min_players}
        logger.info("[igdb] Step 2 - multiplayer modes >= %d players: %d/%d",
                     min_players, len(matching), len(igdb_to_max))
        if not matching:
            return []

        # Step 3: Get Steam app IDs for matching games
        matching_ids = list(matching.keys())
        # Query external_games in batches
        igdb_to_steam: dict[int, int] = {}
        for i in range(0, len(matching_ids), self.MAX_BATCH_SIZE):
            batch = matching_ids[i : i + self.MAX_BATCH_SIZE]
            ids_str = ",".join(str(gid) for gid in batch)
            query = (
                f"fields uid, game; "
                f"where game = ({ids_str}) & category = 1; "
                f"limit 500;"
            )
            rows = await self._query("external_games", query)
            for row in rows:
                try:
                    igdb_to_steam[int(row["game"])] = int(row["uid"])
                except (ValueError, TypeError, KeyError):
                    continue

        logger.info("[igdb] Step 3 - have Steam app IDs: %d/%d", len(igdb_to_steam), len(matching))

        # Step 4: Combine
        results = []
        for gid, steam_app_id in igdb_to_steam.items():
            results.append({
                "name": game_names.get(gid, "Unknown"),
                "steam_app_id": steam_app_id,
                "max_players": matching[gid],
            })

        results.sort(key=lambda g: (-g["max_players"], g["name"]))
        return results[:limit]

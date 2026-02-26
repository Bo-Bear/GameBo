import json
import logging
from pathlib import Path
from typing import Optional

from steam_api import SteamAPI
from igdb_client import IGDBClient
from database import Database

logger = logging.getLogger(__name__)

OVERRIDES_PATH = Path(__file__).parent / "player_overrides.json"


class GameFinder:
    """Orchestrates scanning Steam libraries, fetching player counts, and finding common games."""

    def __init__(self, steam: SteamAPI, igdb: IGDBClient, db: Database):
        self.steam = steam
        self.igdb = igdb
        self.db = db

    def _load_overrides(self) -> dict[int, int]:
        """Load manual max player count overrides from JSON file.

        File format: { "app_id_as_string": max_players, ... }
        """
        if not OVERRIDES_PATH.exists():
            return {}
        with open(OVERRIDES_PATH) as f:
            raw = json.load(f)
        return {int(k): v for k, v in raw.items()}

    async def scan_gamer(self, steam_id: str) -> int:
        """Scan a single gamer's Steam library. Returns the number of games found."""
        games = await self.steam.get_owned_games(steam_id)

        # Save all games to database
        await self.db.upsert_games_bulk(games)

        # Save the gamer's library
        app_ids = [g["app_id"] for g in games]
        await self.db.set_gamer_library(steam_id, app_ids)

        return len(games)

    async def fetch_player_counts(self, progress_callback=None) -> int:
        """Fetch max player counts from IGDB for games that are missing them.

        Applies manual overrides first, then queries IGDB for the rest.
        Returns the number of games updated.
        """
        # Apply manual overrides first
        overrides = self._load_overrides()
        if overrides:
            await self.db.update_max_players_bulk(overrides)

        # Find games still missing player counts
        missing = await self.db.get_games_missing_player_count()
        if not missing:
            return len(overrides)

        if progress_callback:
            await progress_callback(f"Looking up player counts for {len(missing)} games...")

        # Query IGDB in batches
        igdb_results = await self.igdb.get_max_players_batch(missing)
        if igdb_results:
            await self.db.update_max_players_bulk(igdb_results)

        return len(overrides) + len(igdb_results)

    async def find_common_games(
        self, steam_ids: list[str], min_players: Optional[int] = None
    ) -> list[dict]:
        """Find games that all specified gamers own with enough player support.

        If min_players is None, defaults to the number of gamers.
        """
        if min_players is None:
            min_players = len(steam_ids)

        return await self.db.find_common_games(steam_ids, min_players)

    async def find_free_multiplayer_games(
        self, min_players: int, limit: int = 25
    ) -> list[dict]:
        """Find free-to-play games that support at least min_players players.

        Pipeline (Steam-first for reliable pricing):
        1. Scrape Steam store search for free games (authoritative source)
        2. Confirm free + get names via appdetails
        3. Map Steam app IDs -> IGDB game IDs via external_games
        4. Get multiplayer mode data from IGDB
        5. Filter by min_players and return

        Returns a list of dicts with keys: name, app_id, max_players.
        """
        # Step 1: Get free games from Steam search (names included, no appdetails needed)
        free_games = await self.steam.search_free_games()
        logger.info("[freegames] Step 1 - Steam search: %d free games", len(free_games))
        if not free_games:
            return []

        # Build app_id -> name lookup
        app_names = {g["app_id"]: g["name"] for g in free_games}
        confirmed_ids = list(app_names.keys())

        # Step 3: Map Steam app IDs -> IGDB game IDs
        appid_to_igdb = await self.igdb.map_steam_appids(confirmed_ids)
        logger.info("[freegames] Step 3 - IGDB mapped: %d/%d have IGDB entries", len(appid_to_igdb), len(confirmed_ids))
        if not appid_to_igdb:
            return []

        # Step 4: Get multiplayer modes from IGDB
        igdb_ids = list(set(appid_to_igdb.values()))
        igdb_to_max = await self.igdb.get_multiplayer_max_players(igdb_ids)
        logger.info("[freegames] Step 4 - multiplayer modes: %d/%d have data", len(igdb_to_max), len(igdb_ids))

        # Step 5: Join and filter by min_players
        results = []
        for app_id, igdb_id in appid_to_igdb.items():
            max_players = igdb_to_max.get(igdb_id)
            if max_players is None or max_players < min_players:
                continue
            results.append({
                "name": app_names.get(app_id, f"App {app_id}"),
                "app_id": app_id,
                "max_players": max_players,
            })

        logger.info("[freegames] Step 5 - final results: %d games with %d+ players", len(results), min_players)
        results.sort(key=lambda g: (-g["max_players"], g["name"]))
        return results[:limit]

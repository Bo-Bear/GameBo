import json
from pathlib import Path
from typing import Optional

from steam_api import SteamAPI
from igdb_client import IGDBClient
from database import Database

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

        Searches Steam store for free multiplayer games, then queries IGDB
        for player count data to filter by the requested player count.
        Returns a list of dicts with keys: name, app_id, max_players.
        """
        # Step 1: Get free multiplayer games from Steam store
        free_games = await self.steam.search_free_multiplayer_games()
        if not free_games:
            return []

        # Step 2: Look up player counts from IGDB
        app_ids = [g["app_id"] for g in free_games]
        player_counts = await self.igdb.get_max_players_batch(app_ids)

        # Step 3: Combine and filter by requested player count
        results = []
        for game in free_games:
            max_players = player_counts.get(game["app_id"], 0)
            if max_players >= min_players:
                results.append({
                    "name": game["name"],
                    "app_id": game["app_id"],
                    "max_players": max_players,
                })

        results.sort(key=lambda g: (-g["max_players"], g["name"]))
        return results[:limit]

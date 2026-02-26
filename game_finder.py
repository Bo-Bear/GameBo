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

    async def fetch_player_counts(
        self, target_app_ids: list[int] | None = None, progress_callback=None
    ) -> int:
        """Fetch max player counts for games that are missing them.

        If target_app_ids is given, uses the Steam Store API as the primary
        source (accurate but rate-limited to ~1 req/sec — best for small lists
        like common games).  Falls back to IGDB for exact player counts.

        If target_app_ids is None (e.g. /refresh), uses IGDB only (fast bulk
        lookup but sparse coverage).

        Returns the number of games updated.
        """
        # Apply manual overrides first
        overrides = self._load_overrides()
        if overrides:
            await self.db.update_max_players_bulk(overrides)

        # Find games still missing player counts
        all_missing = set(await self.db.get_games_missing_player_count())
        if target_app_ids is not None:
            missing = [aid for aid in target_app_ids if aid in all_missing]
        else:
            missing = list(all_missing)

        logger.info("[player_counts] Games missing player count: %d", len(missing))
        if not missing:
            return len(overrides)

        updated = 0

        if target_app_ids is not None:
            # ── Targeted mode (gamenight): Steam primary, IGDB secondary ──
            if progress_callback:
                await progress_callback(
                    f"Checking multiplayer info for {len(missing)} games via Steam Store..."
                )

            steam_results = await self.steam.get_multiplayer_tags(missing)
            logger.info(
                "[player_counts] Steam returned data for %d/%d games",
                len(steam_results), len(missing),
            )
            if steam_results:
                await self.db.update_max_players_bulk(steam_results)
                updated += len(steam_results)

            # Try IGDB for exact player counts on multiplayer games
            multiplayer_ids = [aid for aid, mp in steam_results.items() if mp != 1]
            if multiplayer_ids:
                try:
                    steam_names = await self.db.get_game_names(multiplayer_ids)
                    igdb_results = await self.igdb.get_max_players_batch(
                        multiplayer_ids, steam_names=steam_names
                    )
                    if igdb_results:
                        await self.db.update_max_players_bulk(igdb_results)
                        logger.info(
                            "[player_counts] IGDB provided counts for %d games",
                            len(igdb_results),
                        )
                except Exception as e:
                    logger.warning("[player_counts] IGDB lookup failed (non-critical): %s", e)
        else:
            # ── Bulk mode (/refresh): IGDB only ──
            if progress_callback:
                await progress_callback(
                    f"Looking up player counts for {len(missing)} games via IGDB..."
                )

            igdb_results = await self.igdb.get_max_players_batch(missing)
            logger.info(
                "[player_counts] IGDB returned player data for %d/%d games",
                len(igdb_results), len(missing),
            )
            if igdb_results:
                await self.db.update_max_players_bulk(igdb_results)
                updated += len(igdb_results)

        return len(overrides) + updated

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

        Hybrid pipeline (IGDB for multiplayer, Steam for pricing):
        1. IGDB: find popular multiplayer games with Steam app IDs
        2. Steam appdetails: check shortlist for is_free (sequential, 1/sec)

        Returns a list of dicts with keys: name, app_id, max_players.
        """
        # Step 1: Get multiplayer game candidates from IGDB (all separate queries)
        candidates = await self.igdb.find_multiplayer_games(min_players)
        logger.info("[freegames] Step 1 - IGDB multiplayer candidates: %d", len(candidates))
        if not candidates:
            return []

        # Step 2: Check which are free on Steam (sequential, 1 per second)
        app_ids = [g["steam_app_id"] for g in candidates]
        free_ids = await self.steam.check_free_apps(app_ids)
        logger.info("[freegames] Step 2 - Steam free check: %d/%d are free", len(free_ids), len(app_ids))

        # Step 3: Filter to free games only
        results = [
            {
                "name": g["name"],
                "app_id": g["steam_app_id"],
                "max_players": g["max_players"],
            }
            for g in candidates
            if g["steam_app_id"] in free_ids
        ]

        logger.info("[freegames] Step 3 - final results: %d free games with %d+ players", len(results), min_players)
        results.sort(key=lambda g: (-g["max_players"], g["name"]))
        return results[:limit]

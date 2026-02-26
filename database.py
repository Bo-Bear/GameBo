import logging
import aiosqlite
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "gamebo.db"


class Database:
    """SQLite database for caching game data and gamer libraries."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self):
        if self._db:
            await self._db.close()

    async def _create_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS games (
                app_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                max_players INTEGER DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS gamer_games (
                steam_id TEXT NOT NULL,
                app_id INTEGER NOT NULL,
                PRIMARY KEY (steam_id, app_id)
            );
        """)
        await self._db.commit()

    # -- Game operations --

    async def upsert_game(self, app_id: int, name: str, max_players: Optional[int] = None):
        """Insert or update a game. Does not overwrite max_players if new value is None."""
        if max_players is not None:
            await self._db.execute(
                "INSERT INTO games (app_id, name, max_players) VALUES (?, ?, ?) "
                "ON CONFLICT(app_id) DO UPDATE SET name = excluded.name, max_players = excluded.max_players",
                (app_id, name, max_players),
            )
        else:
            await self._db.execute(
                "INSERT INTO games (app_id, name) VALUES (?, ?) "
                "ON CONFLICT(app_id) DO UPDATE SET name = excluded.name",
                (app_id, name),
            )
        await self._db.commit()

    async def upsert_games_bulk(self, games: list[dict]):
        """Bulk insert/update games. Each dict has: app_id, name, optionally max_players."""
        for game in games:
            await self.upsert_game(
                game["app_id"],
                game["name"],
                game.get("max_players"),
            )

    async def update_max_players_bulk(self, player_counts: dict[int, int]):
        """Bulk update max_players for games by app_id."""
        for app_id, max_players in player_counts.items():
            await self._db.execute(
                "UPDATE games SET max_players = ? WHERE app_id = ?",
                (max_players, app_id),
            )
        await self._db.commit()

    async def get_game(self, app_id: int) -> Optional[dict]:
        async with self._db.execute(
            "SELECT app_id, name, max_players FROM games WHERE app_id = ?",
            (app_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"app_id": row["app_id"], "name": row["name"], "max_players": row["max_players"]}
        return None

    async def get_games_missing_player_count(self) -> list[int]:
        """Get app_ids of games that don't have a max_players value yet."""
        async with self._db.execute(
            "SELECT app_id FROM games WHERE max_players IS NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            return [row["app_id"] for row in rows]

    async def get_game_names(self, app_ids: list[int]) -> dict[int, str]:
        """Get game names for a list of app_ids."""
        if not app_ids:
            return {}
        placeholders = ",".join("?" for _ in app_ids)
        query = f"SELECT app_id, name FROM games WHERE app_id IN ({placeholders})"
        async with self._db.execute(query, app_ids) as cursor:
            rows = await cursor.fetchall()
            return {row["app_id"]: row["name"] for row in rows}

    # -- Gamer library operations --

    async def set_gamer_library(self, steam_id: str, app_ids: list[int]):
        """Replace a gamer's entire library with the given app_ids."""
        await self._db.execute(
            "DELETE FROM gamer_games WHERE steam_id = ?", (steam_id,)
        )
        for app_id in app_ids:
            await self._db.execute(
                "INSERT OR IGNORE INTO gamer_games (steam_id, app_id) VALUES (?, ?)",
                (steam_id, app_id),
            )
        await self._db.commit()

    async def get_gamer_library(self, steam_id: str) -> list[dict]:
        """Get all games owned by a gamer, with name and max_players."""
        async with self._db.execute(
            """
            SELECT g.app_id, g.name, g.max_players
            FROM gamer_games gg
            JOIN games g ON gg.app_id = g.app_id
            WHERE gg.steam_id = ?
            ORDER BY g.name
            """,
            (steam_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"app_id": row["app_id"], "name": row["name"], "max_players": row["max_players"]}
                for row in rows
            ]

    async def find_common_game_ids(self, steam_ids: list[str]) -> list[int]:
        """Find app_ids owned by ALL given gamers (ignoring player count)."""
        if not steam_ids:
            return []
        placeholders = ",".join("?" for _ in steam_ids)
        query = f"""
            SELECT g.app_id
            FROM gamer_games gg
            JOIN games g ON gg.app_id = g.app_id
            WHERE gg.steam_id IN ({placeholders})
            GROUP BY g.app_id
            HAVING COUNT(DISTINCT gg.steam_id) = ?
        """
        async with self._db.execute(query, [*steam_ids, len(steam_ids)]) as cursor:
            rows = await cursor.fetchall()
            return [row["app_id"] for row in rows]

    async def find_common_games(self, steam_ids: list[str], min_players: int) -> list[dict]:
        """Find games owned by ALL given gamers with max_players >= min_players.

        Returns a list of dicts with: app_id, name, max_players.
        """
        if not steam_ids:
            return []

        placeholders = ",".join("?" for _ in steam_ids)
        query = f"""
            SELECT g.app_id, g.name, g.max_players
            FROM gamer_games gg
            JOIN games g ON gg.app_id = g.app_id
            WHERE gg.steam_id IN ({placeholders})
              AND g.max_players IS NOT NULL
              AND g.max_players >= ?
            GROUP BY g.app_id
            HAVING COUNT(DISTINCT gg.steam_id) = ?
            ORDER BY g.name
        """
        params = [*steam_ids, min_players, len(steam_ids)]

        # Debug: count common games ignoring player count
        debug_query = f"""
            SELECT COUNT(DISTINCT g.app_id) as cnt
            FROM gamer_games gg
            JOIN games g ON gg.app_id = g.app_id
            WHERE gg.steam_id IN ({placeholders})
            GROUP BY g.app_id
            HAVING COUNT(DISTINCT gg.steam_id) = ?
        """
        async with self._db.execute(debug_query, [*steam_ids, len(steam_ids)]) as cursor:
            debug_rows = await cursor.fetchall()
            logger.info("[db] Common games (all %d gamers own): %d", len(steam_ids), len(debug_rows))

        # Debug: how many of those have player count data
        debug_query2 = f"""
            SELECT COUNT(DISTINCT g.app_id) as cnt
            FROM gamer_games gg
            JOIN games g ON gg.app_id = g.app_id
            WHERE gg.steam_id IN ({placeholders})
              AND g.max_players IS NOT NULL
            GROUP BY g.app_id
            HAVING COUNT(DISTINCT gg.steam_id) = ?
        """
        async with self._db.execute(debug_query2, [*steam_ids, len(steam_ids)]) as cursor:
            debug_rows2 = await cursor.fetchall()
            logger.info("[db] Common games with player data: %d", len(debug_rows2))

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            logger.info("[db] Common games with %d+ players: %d", min_players, len(rows))
            return [
                {"app_id": row["app_id"], "name": row["name"], "max_players": row["max_players"]}
                for row in rows
            ]

    async def find_common_games_unknown(self, steam_ids: list[str]) -> list[dict]:
        """Find games owned by ALL gamers with max_players = -1 (multiplayer, count unknown).

        These are games Steam confirmed as multiplayer but no exact player
        count is available from IGDB or overrides.
        """
        if not steam_ids:
            return []
        placeholders = ",".join("?" for _ in steam_ids)
        query = f"""
            SELECT g.app_id, g.name, g.max_players
            FROM gamer_games gg
            JOIN games g ON gg.app_id = g.app_id
            WHERE gg.steam_id IN ({placeholders})
              AND g.max_players = -1
            GROUP BY g.app_id
            HAVING COUNT(DISTINCT gg.steam_id) = ?
            ORDER BY g.name
        """
        async with self._db.execute(query, [*steam_ids, len(steam_ids)]) as cursor:
            rows = await cursor.fetchall()
            return [
                {"app_id": row["app_id"], "name": row["name"], "max_players": row["max_players"]}
                for row in rows
            ]

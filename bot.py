import json
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from steam_api import SteamAPI
from igdb_client import IGDBClient
from database import Database
from game_finder import GameFinder

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
GAMERS_PATH = Path(__file__).parent / "gamers.json"


def load_gamers() -> list[dict]:
    """Load the gamer list from gamers.json."""
    if not GAMERS_PATH.exists():
        return []
    with open(GAMERS_PATH) as f:
        data = json.load(f)
    return data.get("gamers", [])


def get_gamer_names() -> list[str]:
    return [g["name"] for g in load_gamers()]


def get_gamer_by_name(name: str) -> dict | None:
    for g in load_gamers():
        if g["name"].lower() == name.lower():
            return g
    return None


# -- Bot setup --

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# These get initialized in on_ready
steam: SteamAPI
igdb: IGDBClient
db: Database
finder: GameFinder


@bot.event
async def on_ready():
    global steam, igdb, db, finder

    steam = SteamAPI(STEAM_API_KEY)
    igdb = IGDBClient(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
    db = Database()
    await db.connect()
    finder = GameFinder(steam, igdb, db)

    await bot.tree.sync()
    print(f"GameBo is online as {bot.user}")


# -- Autocomplete helper --

async def gamer_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete for gamer names."""
    names = get_gamer_names()
    return [
        app_commands.Choice(name=n, value=n)
        for n in names
        if current.lower() in n.lower()
    ][:25]


# -- Slash commands --


@bot.tree.command(name="gamers", description="List all configured gamers")
async def cmd_gamers(interaction: discord.Interaction):
    gamers = load_gamers()
    if not gamers:
        await interaction.response.send_message(
            "No gamers configured yet. Add them to `gamers.json`.", ephemeral=True
        )
        return

    embed = discord.Embed(title="Configured Gamers", color=0x1B2838)
    for g in gamers:
        steam_id = g["steam_id"]
        profile_url = f"https://steamcommunity.com/profiles/{steam_id}"
        embed.add_field(
            name=g["name"],
            value=f"[Steam Profile]({profile_url})\n`{steam_id}`",
            inline=True,
        )
    embed.set_footer(text=f"{len(gamers)} gamer(s) configured")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="scan", description="Scan Steam libraries for all gamers (or one specific gamer)")
@app_commands.describe(gamer="Optional: scan only this gamer's library")
@app_commands.autocomplete(gamer=gamer_autocomplete)
async def cmd_scan(interaction: discord.Interaction, gamer: str | None = None):
    await interaction.response.defer(thinking=True)

    gamers_to_scan = []
    if gamer:
        g = get_gamer_by_name(gamer)
        if not g:
            await interaction.followup.send(f"Gamer **{gamer}** not found in config.")
            return
        gamers_to_scan = [g]
    else:
        gamers_to_scan = load_gamers()

    if not gamers_to_scan:
        await interaction.followup.send("No gamers configured. Add them to `gamers.json`.")
        return

    results = []
    for g in gamers_to_scan:
        try:
            count = await finder.scan_gamer(g["steam_id"])
            results.append(f"**{g['name']}**: {count} games found")
        except Exception as e:
            results.append(f"**{g['name']}**: Error - {e}")

    # Fetch player counts from IGDB
    await interaction.followup.send(
        "\n".join(results) + "\n\nFetching player count data from IGDB..."
    )

    try:
        updated = await finder.fetch_player_counts()
        await interaction.channel.send(f"Player count data updated for {updated} game(s). Scan complete!")
    except Exception as e:
        await interaction.channel.send(f"Warning: Could not fetch player counts from IGDB: {e}")


@bot.tree.command(name="findgames", description="Find co-op games that all selected gamers own")
@app_commands.describe(
    gamers="Comma-separated gamer names (e.g. Alice, Bob, Charlie)",
    min_players="Minimum player count (defaults to number of gamers)",
)
async def cmd_findgames(
    interaction: discord.Interaction,
    gamers: str,
    min_players: int | None = None,
):
    await interaction.response.defer(thinking=True)

    # Parse gamer names
    names = [n.strip() for n in gamers.split(",") if n.strip()]
    if len(names) < 2:
        await interaction.followup.send("Please provide at least 2 gamer names, separated by commas.")
        return

    # Resolve names to steam IDs
    steam_ids = []
    resolved_names = []
    for name in names:
        g = get_gamer_by_name(name)
        if not g:
            await interaction.followup.send(
                f"Gamer **{name}** not found. Use `/gamers` to see configured gamers."
            )
            return
        steam_ids.append(g["steam_id"])
        resolved_names.append(g["name"])

    player_count = min_players if min_players else len(steam_ids)
    common_games = await finder.find_common_games(steam_ids, player_count)

    if not common_games:
        await interaction.followup.send(
            f"No games found that all **{len(resolved_names)}** gamers own "
            f"with **{player_count}+** player support.\n\n"
            f"Tip: Run `/scan` first to ensure libraries and player counts are up to date."
        )
        return

    # Build response embed(s)
    embed = discord.Embed(
        title=f"Co-op Games for {', '.join(resolved_names)}",
        description=f"Games all {len(resolved_names)} gamers own with {player_count}+ player support:",
        color=0x66C0F4,
    )

    # Chunk games into the embed (Discord limit: 4096 chars for description)
    game_lines = []
    for g in common_games:
        store_url = f"https://store.steampowered.com/app/{g['app_id']}"
        game_lines.append(f"[{g['name']}]({store_url}) — up to **{g['max_players']}** players")

    # If too many games, paginate into fields
    chunk_size = 15
    if len(game_lines) <= chunk_size:
        embed.description += "\n\n" + "\n".join(game_lines)
    else:
        embed.description += f"\n\n**{len(game_lines)} games found:**"
        for i in range(0, len(game_lines), chunk_size):
            chunk = game_lines[i : i + chunk_size]
            field_name = f"Page {i // chunk_size + 1}"
            embed.add_field(name=field_name, value="\n".join(chunk), inline=False)

    embed.set_footer(text=f"{len(common_games)} game(s) found")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="override", description="Manually set the max player count for a game")
@app_commands.describe(
    game_name="Name of the game (search by partial match)",
    max_players="Maximum number of co-op players",
)
async def cmd_override(interaction: discord.Interaction, game_name: str, max_players: int):
    if max_players < 1:
        await interaction.response.send_message("Max players must be at least 1.", ephemeral=True)
        return

    # Search for matching games in the database
    await interaction.response.defer(ephemeral=True)

    # Load overrides file
    overrides_path = Path(__file__).parent / "player_overrides.json"
    overrides = {}
    if overrides_path.exists():
        with open(overrides_path) as f:
            overrides = json.load(f)

    # Find matching games from the database
    from database import Database as _DB

    async with db._db.execute(
        "SELECT app_id, name FROM games WHERE LOWER(name) LIKE ?",
        (f"%{game_name.lower()}%",),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        await interaction.followup.send(
            f"No games found matching **{game_name}**. Run `/scan` first."
        )
        return

    if len(rows) > 10:
        matches = "\n".join(f"• {r['name']}" for r in rows[:10])
        await interaction.followup.send(
            f"Too many matches ({len(rows)}). Be more specific:\n{matches}\n..."
        )
        return

    # If exactly one match, apply it directly
    if len(rows) == 1:
        app_id = rows[0]["app_id"]
        name = rows[0]["name"]
        overrides[str(app_id)] = max_players
        with open(overrides_path, "w") as f:
            json.dump(overrides, f, indent=2)
        await db.update_max_players_bulk({app_id: max_players})
        await interaction.followup.send(
            f"Set **{name}** max players to **{max_players}**."
        )
        return

    # Multiple matches — show them and ask user to be more specific
    matches = "\n".join(f"• {r['name']}" for r in rows)
    await interaction.followup.send(
        f"Multiple matches found. Be more specific:\n{matches}"
    )


@bot.tree.command(name="gameinfo", description="Show info about a specific game")
@app_commands.describe(game_name="Name of the game (search by partial match)")
async def cmd_gameinfo(interaction: discord.Interaction, game_name: str):
    await interaction.response.defer()

    async with db._db.execute(
        "SELECT app_id, name, max_players FROM games WHERE LOWER(name) LIKE ?",
        (f"%{game_name.lower()}%",),
    ) as cursor:
        rows = await cursor.fetchall()

    if not rows:
        await interaction.followup.send(f"No games found matching **{game_name}**. Run `/scan` first.")
        return

    # Show top 5 matches
    embed = discord.Embed(title=f"Search: {game_name}", color=0x1B2838)
    for row in rows[:5]:
        store_url = f"https://store.steampowered.com/app/{row['app_id']}"
        mp = row["max_players"]
        player_info = f"**{mp}** players" if mp else "Unknown"
        embed.add_field(
            name=row["name"],
            value=f"Max co-op: {player_info}\n[Store page]({store_url})",
            inline=False,
        )

    if len(rows) > 5:
        embed.set_footer(text=f"Showing 5 of {len(rows)} matches")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="refresh", description="Re-fetch player counts from IGDB for games missing data")
async def cmd_refresh(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    try:
        updated = await finder.fetch_player_counts()
        await interaction.followup.send(f"Player count data updated for **{updated}** game(s).")
    except Exception as e:
        await interaction.followup.send(f"Error fetching player counts: {e}")


# -- Shutdown --

@bot.event
async def on_close():
    await steam.close()
    await igdb.close()
    await db.close()


# -- Entry point --

def main():
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN not set. Check your .env file.")
        return
    if not STEAM_API_KEY:
        print("Error: STEAM_API_KEY not set. Check your .env file.")
        return
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        print("Error: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET not set. Check your .env file.")
        return

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

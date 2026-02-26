import json
import logging
import os
from pathlib import Path

import discord

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
from discord.ext import commands
from dotenv import load_dotenv

from steam_api import SteamAPI
from igdb_client import IGDBClient
from database import Database
from game_finder import GameFinder

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
GUILD_ID = os.getenv("GUILD_ID", "")
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

    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    print(f"GameBo is online as {bot.user}")


# -- Slash commands --

class GamerSelect(discord.ui.Select):
    """Multi-select dropdown for picking gamers."""

    def __init__(self):
        gamers = load_gamers()
        options = [
            discord.SelectOption(label=g["name"], value=g["name"])
            for g in gamers
        ][:25]  # Discord max 25 options
        super().__init__(
            placeholder="Select gamers for game night...",
            min_values=2,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        selected_names = self.values
        await interaction.response.defer(thinking=True)

        # Resolve names to gamer dicts
        selected_gamers = []
        for name in selected_names:
            g = get_gamer_by_name(name)
            if g:
                selected_gamers.append(g)

        if len(selected_gamers) < 2:
            await interaction.followup.send("Could not resolve selected gamers.")
            return

        # Step 1: Scan libraries for selected gamers
        scan_lines = []
        for g in selected_gamers:
            try:
                count = await finder.scan_gamer(g["steam_id"])
                scan_lines.append(f"**{g['name']}**: {count} games")
            except Exception as e:
                scan_lines.append(f"**{g['name']}**: Error - {e}")

        steam_ids = [g["steam_id"] for g in selected_gamers]
        resolved_names = [g["name"] for g in selected_gamers]
        player_count = len(steam_ids)

        # Step 2: Find common games (unfiltered) to narrow down lookups
        common_ids = await db.find_common_game_ids(steam_ids)

        await interaction.followup.send(
            "**Scanning libraries...**\n"
            + "\n".join(scan_lines)
            + f"\n\n{len(common_ids)} games in common — checking multiplayer info..."
        )

        # Step 3: Fetch player counts for common games only (Steam + IGDB)
        try:
            await finder.fetch_player_counts(target_app_ids=common_ids)
        except Exception as e:
            await interaction.channel.send(f"Warning: Could not fetch player counts: {e}")

        # Step 4: Find common games — known counts + unknown multiplayer
        common_games = await finder.find_common_games(steam_ids, player_count)
        unknown_games = await db.find_common_games_unknown(steam_ids)

        if not common_games and not unknown_games:
            await interaction.channel.send(
                f"No games found that all **{len(resolved_names)}** gamers own "
                f"with multiplayer support."
            )
            return

        # Build response embed
        embed = discord.Embed(
            title=f"Game Night: {', '.join(resolved_names)}",
            color=0x66C0F4,
        )

        # Section 1: Games with confirmed player counts
        if common_games:
            game_lines = []
            for g in common_games:
                store_url = f"https://store.steampowered.com/app/{g['app_id']}"
                game_lines.append(
                    f"[{g['name']}]({store_url}) — up to **{g['max_players']}** players"
                )

            embed.description = (
                f"Games all {len(resolved_names)} gamers own with "
                f"{player_count}+ player support:\n\n"
                + "\n".join(game_lines[:15])
            )
            if len(game_lines) > 15:
                remaining = game_lines[15:]
                current_chunk = []
                for line in remaining:
                    candidate = "\n".join(current_chunk + [line])
                    if len(candidate) > 1024 and current_chunk:
                        embed.add_field(
                            name="\u200b",
                            value="\n".join(current_chunk),
                            inline=False,
                        )
                        current_chunk = [line]
                    else:
                        current_chunk.append(line)
                if current_chunk:
                    embed.add_field(
                        name="\u200b",
                        value="\n".join(current_chunk),
                        inline=False,
                    )
        else:
            embed.description = (
                f"No games with confirmed {player_count}+ player support found."
            )

        # Section 2: Multiplayer games with unknown player count
        if unknown_games:
            suffix = "\n\nEdit `player_overrides.json` to set player counts."
            unknown_lines = []
            for g in unknown_games:
                store_url = f"https://store.steampowered.com/app/{g['app_id']}"
                line = f"[{g['name']}]({store_url})"
                # Stop adding if the next line would exceed Discord's 1024-char field limit
                candidate = "\n".join(unknown_lines + [line]) + suffix
                if len(candidate) > 1024:
                    break
                unknown_lines.append(line)

            shown = len(unknown_lines)
            label = "Multiplayer — player count unknown"
            if shown < len(unknown_games):
                label += f" (showing {shown} of {len(unknown_games)})"
            embed.add_field(
                name=label,
                value="\n".join(unknown_lines) + suffix,
                inline=False,
            )

        total = len(common_games) + len(unknown_games)
        embed.set_footer(
            text=f"{len(common_games)} confirmed + {len(unknown_games)} unknown = {total} game(s)"
        )
        await interaction.channel.send(embed=embed)


class GamerSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(GamerSelect())


@bot.tree.command(name="gamenight", description="Pick gamers and find co-op games you all own")
async def cmd_gamenight(interaction: discord.Interaction):
    gamers = load_gamers()
    if len(gamers) < 2:
        await interaction.response.send_message(
            "Need at least 2 gamers in `gamers.json`.", ephemeral=True
        )
        return
    await interaction.response.send_message(
        "**Who's playing tonight?** Select gamers below:",
        view=GamerSelectView(),
    )


class FreeGamesModal(discord.ui.Modal, title="Find Free-to-Play Games"):
    players = discord.ui.TextInput(
        label="Player count",
        placeholder="How many players? (e.g. 4)",
        min_length=1,
        max_length=3,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            count = int(self.players.value)
        except ValueError:
            await interaction.response.send_message("Please enter a valid number.", ephemeral=True)
            return

        if count < 2:
            await interaction.response.send_message("Please specify at least 2 players.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        try:
            games = await finder.find_free_multiplayer_games(count)
        except Exception as e:
            await interaction.followup.send(f"Error searching for games: {e}")
            return

        if not games:
            await interaction.followup.send(
                f"No free-to-play games found supporting **{count}+** players."
            )
            return

        embed = discord.Embed(
            title=f"Free-to-Play Games for {count}+ Players",
            color=0x2ECC71,
        )

        game_lines = []
        for g in games:
            store_url = f"https://store.steampowered.com/app/{g['app_id']}"
            game_lines.append(f"[{g['name']}]({store_url}) — up to **{g['max_players']}** players")

        if len(game_lines) <= 15 and len("\n".join(game_lines)) <= 4096:
            embed.description = "\n".join(game_lines)
        else:
            embed.description = f"**{len(game_lines)} games found:**"
            current_chunk = []
            for line in game_lines:
                candidate = "\n".join(current_chunk + [line])
                if len(candidate) > 1024 and current_chunk:
                    embed.add_field(
                        name="\u200b",
                        value="\n".join(current_chunk),
                        inline=False,
                    )
                    current_chunk = [line]
                else:
                    current_chunk.append(line)
            if current_chunk:
                embed.add_field(
                    name="\u200b",
                    value="\n".join(current_chunk),
                    inline=False,
                )

        embed.set_footer(text=f"{len(games)} game(s) found")
        await interaction.followup.send(embed=embed)


@bot.tree.command(name="freegames", description="Find free-to-play games for a given number of players")
async def cmd_freegames(interaction: discord.Interaction):
    await interaction.response.send_modal(FreeGamesModal())


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

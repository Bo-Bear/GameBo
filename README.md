# GameBo

A Discord bot that scans Steam libraries and finds co-op games your group can play together.

Given a list of gamers, GameBo will:
1. Pull each gamer's Steam library
2. Look up max co-op player counts (via IGDB)
3. Find games **all** selected gamers own that support enough players

## Prerequisites

You'll need four free API credentials:

| Credential | Where to get it |
|---|---|
| **Discord Bot Token** | [Discord Developer Portal](https://discord.com/developers/applications) — create an app, add a bot, copy the token |
| **Steam API Key** | [Steam Web API Key](https://steamcommunity.com/dev/apikey) — log in and register |
| **Twitch Client ID** | [Twitch Developer Console](https://dev.twitch.tv/console) — register an app |
| **Twitch Client Secret** | Same Twitch app — generate a client secret |

## Setup

### 1. Clone and install dependencies

```bash
git clone <this-repo>
cd GameBo
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```
DISCORD_TOKEN=your_discord_bot_token
STEAM_API_KEY=your_steam_api_key
TWITCH_CLIENT_ID=your_twitch_client_id
TWITCH_CLIENT_SECRET=your_twitch_client_secret
```

### 3. Add your gamers

Edit `gamers.json` with your group:

```json
{
  "gamers": [
    {
      "name": "Alice",
      "steam_id": "76561198000000001"
    },
    {
      "name": "Bob",
      "steam_id": "76561198000000002"
    }
  ]
}
```

**Finding a Steam ID:** Go to someone's Steam profile. If the URL is `steamcommunity.com/profiles/76561198000000001`, that number is their Steam ID. If they have a vanity URL like `steamcommunity.com/id/username`, you can look up their ID at [steamid.io](https://steamid.io).

**Important:** Each gamer's Steam profile must have their **game details set to public** (Steam Settings → Privacy → Game details → Public).

### 4. Invite the bot to your Discord server

In the Discord Developer Portal, go to your app → OAuth2 → URL Generator:
- Scopes: `bot`, `applications.commands`
- Permissions: `Send Messages`, `Embed Links`

Use the generated URL to invite the bot.

### 5. Run the bot

```bash
python bot.py
```

## Commands

| Command | Description |
|---|---|
| `/gamers` | List all configured gamers |
| `/scan [gamer]` | Scan Steam libraries and fetch player counts. Optionally scan just one gamer. |
| `/findgames <gamers> [min_players]` | Find co-op games all listed gamers own. Comma-separated names. |
| `/gameinfo <game_name>` | Look up info about a specific game |
| `/override <game_name> <max_players>` | Manually set a game's max player count |
| `/refresh` | Re-fetch player counts from IGDB for games missing data |

## Example Workflow

1. Run `/scan` to pull everyone's Steam libraries and fetch player counts
2. Run `/findgames gamers: Alice, Bob, Charlie` to find games all three own with 3+ player support
3. If a game has wrong player data, use `/override` to fix it

## Manual Player Count Overrides

IGDB data isn't always perfect. To manually set player counts, either:

- Use the `/override` command in Discord
- Edit `player_overrides.json` directly:

```json
{
  "730": 5,
  "570": 10
}
```

Keys are Steam app IDs (as strings), values are max player counts. Overrides take priority over IGDB data.

## Architecture

```
bot.py              → Discord bot & slash commands
steam_api.py        → Steam Web API client
igdb_client.py      → IGDB/Twitch API client (player counts)
game_finder.py      → Core orchestration logic
database.py         → SQLite caching layer
gamers.json         → Your gamer list
player_overrides.json → Manual player count corrections
```

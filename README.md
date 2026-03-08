# zkill-bot

A Discord bot that fetches EVE Online killmail data from [zKillboard](https://zkillboard.com) and [ESI](https://esi.evetech.net), filtered to NullSec and LowSec space.

## Features

- `/kills` — Interactive filter form (ship categories + time range) that posts a kill report embed to the channel
- `/ping` — Health check
- **Automatic daily summary** — Posts a scheduled kill report to a configured channel every 24 hours
- Tracks: total kills, total ISK destroyed, and up to 10 kills with pilot names and zKillboard links

## Ship Categories

| Key | Label | Ships included |
|---|---|---|
| `industrial` | Industrial & Hauling | T1 Industrials, Blockade Runners, Deep Space Transports, Freighters, Jump Freighters |
| `mining` | Mining | Mining Barges, Exhumers, Mining Frigates (Venture/Prospect/Endurance/Outrider/Pioneer Consortium Issue), Porpoise, Orca, Rorqual, Noctis |
| `exploration` | Exploration | Covert Ops Frigates, T3 Cruisers, Expedition Frigates, Astero, Stratios |
| `capital` | Capital Ships | Carriers, Dreadnoughts, Force Auxiliaries, Supercarriers, Titans |
| `pvp_cruiser` | PvP Cruisers & BCs | Heavy Assault Cruisers, Command Ships, Recon Ships |

## Setup

### Requirements

- Python 3.11+
- A Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))

### Install dependencies

```bash
pip install py-cord httpx python-dotenv
```

### Configure

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_bot_token_here
```

Then set these constants at the top of `bot.py`:

```python
DEV_GUILD_ID         = 123456789012345678   # Your server ID for instant slash command registration
AUTO_POST_CHANNEL_ID = 987654321098765432   # Channel for the daily auto-post (0 to disable)
AUTO_POST_CATEGORIES = ["industrial", "mining"]
AUTO_POST_TIME_KEY   = "24h"
```

### Run

```bash
python bot.py
```

## Testing without Discord

Run the fetch pipeline locally and inspect output without starting the bot:

```bash
python test_local.py
```

Results are printed to the console and saved to `killmails.json`.

The standalone test in `fetcher.py` also works:

```bash
python fetcher.py
```

## Project structure

```
zkill-bot/
├── bot.py          # Discord bot, slash commands, UI views, auto-post task
├── fetcher.py      # Data pipeline: zKillboard → ESI → enriched kill list
├── test_local.py   # Local test runner (no Discord required)
└── .env            # DISCORD_TOKEN (not committed)
```

## How the fetch pipeline works

1. **Discover regions** — Queries ESI for all k-space regions, samples one system per region to determine security status, and caches the resulting NullSec/LowSec region list for the lifetime of the process.
2. **Fetch matching kills** — For each selected ship type ID × NullSec/LowSec region, queries zKillboard's ship-filter endpoint (`/api/ship/{typeID}/regionID/{regionID}/...`). Requests run in parallel (up to 5 concurrent) so only kills matching the chosen ship types are returned — no bulk ESI killmail scanning required.
3. **Enrich** — Fetches the full ESI killmail for each matched kill to retrieve the victim's character ID, then resolves all pilot names in a single bulk POST to `/universe/names/`.
4. **Rate limiting** — 1 s delay between zKillboard requests; 200 ms between ESI requests; automatic `Retry-After` backoff on HTTP 429 responses (up to 4 retries).

## Time ranges

`1h` · `6h` · `12h` · `24h` · `48h` · `7d`

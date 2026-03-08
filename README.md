# zkill-bot

A Discord bot that fetches EVE Online killmail data from [zKillboard](https://zkillboard.com) and [ESI](https://esi.evetech.net), filtered to NullSec and LowSec space.

## Features

- `/kills` — Interactive filter form (ship categories + time range) with a live terminal-style progress display; posts a kill report embed plus a copyable pilot list
- `/ping` — Health check
- **Automatic daily summary** — Posts a scheduled kill report to a configured channel every 24 hours
- Tracks: total kills, total ISK destroyed, up to 10 kills with pilot names and zKillboard links
- **Copyable pilot list** — After each report, posts a code-block list of all pilot names ready to paste into an EVE in-game mail
- **Friendly-fire exclusion** — Kills where the victim belongs to your own alliance or corporation are silently dropped, so your recruiter only sees actual recruitment targets

## Ship Categories

| Key | Label | Ships included |
|---|---|---|
| `industrial` | Industrial & Hauling | T1 Industrials, Deep Space Transports, Blockade Runners, Freighters, Jump Freighters |
| `mining` | Mining | Mining Barges (Covetor/Retriever/Procurer), Exhumers (Hulk/Mackinaw/Skiff), Expedition Frigates (Prospect/Endurance), Venture, Outrider, Pioneer Consortium Issue, Porpoise, Orca, Rorqual, Noctis |
| `exploration` | Exploration | Covert Ops Frigates, T3 Cruisers, Expedition Frigates, Astero, Stratios |
| `capital` | Capital Ships | Carriers, Dreadnoughts, Force Auxiliaries, Supercarriers, Titans |
| `pvp_cruiser` | PvP Cruisers & BCs | Heavy Assault Cruisers, Command Ships, Force Recon Ships, Combat Recon Ships |

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

To exclude kills where the victim is in your own alliance or corporation (so they don't appear in the recruiter's pilot list), add these to `.env`:

```env
EXCLUDED_ALLIANCE_IDS=99014523
EXCLUDED_CORP_IDS=98765432,98765433
```

Both accept comma-separated IDs. Omit or leave blank to disable the filter.

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
2. **Fetch matching kills** — For each selected ship group ID (or individual type ID for ships without a clean group) × NullSec/LowSec region, queries zKillboard's group-filter endpoint (`/api/groupID/{groupID}/regionID/{regionID}/...`) or ship-filter endpoint (`/api/shipID/{typeID}/...`). Requests are serialized (1 per second). Using group IDs reduces the typical industrial+mining query from ~2 100 requests to ~770 (~3× faster).
3. **Enrich** — Fetches the full ESI killmail for each matched kill to retrieve the victim's character ID, ship type, alliance, and corporation; filters out victims from excluded alliances or corporations; then resolves all remaining pilot names in a single bulk POST to `/universe/names/`.
4. **Rate limiting** — Strictly 1 req/s to zKillboard (delay applied even on 404 responses); 100 ms between ESI requests; minimum 30 s backoff on HTTP 429; ESI error budget monitored via `X-ESI-Error-Limit-Remain` (pauses 10 s if < 20 remaining).

## Time ranges

`1h` · `6h` · `12h` · `24h` · `48h` · `7d`

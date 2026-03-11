# zkill-bot

A Discord bot that fetches EVE Online killmail data from [zKillboard](https://zkillboard.com) and [ESI](https://esi.evetech.net), with configurable filters for ship categories, time range, security space (Null/Low/Wormhole/High), min ISK, and specific regions.

## Commands

| Command | Description |
|---|---|
| `/scan` | Open the interactive filter form (ship categories, time range, space type, min ISK, regions) |
| `/last [n]` | Re-post the summary embed from a recent scan (1 = latest, up to 5) |
| `/status` | Show the current scan phase and elapsed time |
| `/stop` | Abort the current scan immediately — no results posted |
| `/skip` | Skip remaining zKillboard scanning and post partial results (zkill phase only) |
| `/ping` | Check bot latency |
| `/help [public]` | Show available commands (pass `public: True` to post in channel) |
| `/daily status` *(admin)* | Show current daily report config and next scheduled run time |
| `/daily configure` *(admin)* | Open a form to set categories, time range, space type, and min ISK |
| `/daily channel #ch` *(admin)* | Set the channel where the daily report is posted |
| `/daily toggle` *(admin)* | Enable or disable the scheduled daily report |
| `/daily run` *(admin)* | Trigger the daily report immediately with the current configuration |
| `/exclusions add <name>` *(admin)* | Search ESI for a corp or alliance by exact name and add it to the exclusion list |
| `/exclusions remove` *(admin)* | Pick a corp or alliance to remove via a select menu |
| `/exclusions list` *(admin)* | Show all currently excluded corps and alliances |

## Features

- **Live progress panel** — Public terminal-style status message updated in real time during a scan; automatically deleted when the scan completes
- **Automatic daily summary** — Posts a scheduled kill report to a configured channel every 24 hours
- **Kill report embed** — Total kills, unique pilots, total ISK destroyed, breakdown by category and security space
- **Detailed kill list** — Per-kill breakdown with pilot, corp, ship, ISK, system, timestamp and zKillboard link; falls back to a `kills.txt` file attachment when the list is too long for a Discord message
- **Copyable pilot list** — Code-block list of all pilot names ready to paste into an EVE in-game mail; falls back to `pilot_names.txt` file attachment
- **EVE in-game mail — HTML pilot links** — Pre-formatted HTML with `showinfo` character links for direct paste into the EVE client mail composer; falls back to `eve_mail.txt` file attachment
- **Friendly-fire exclusion** — Kills where the victim belongs to your own alliance or corporation are silently dropped; managed via `/exclusions` commands or `.env` constants
- **Region name filter** — Scan form accepts a comma-separated list of region names to restrict the scan to specific regions regardless of space type
- **HighSec results cap** — Scans that include High Security space automatically cap zKillboard results at `HIGHSEC_MAX_RESULTS` to prevent runaway scan times
- **Hard scan timeout** — Each fetch is limited to 1 hour; `/status` warns if a scan appears stuck

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
MAX_SCAN_RESULTS     = None                 # Cap zKillboard results (e.g. 500); None = no cap
HIGHSEC_MAX_RESULTS  = 200                  # Automatic cap when High Security space is selected
```

To exclude kills where the victim is in your own alliance or corporation (so they don't appear in the recruiter's pilot list), use the `/exclusions add` command (preferred) or add IDs directly to `.env`:

```env
EXCLUDED_ALLIANCE_IDS=99014523
EXCLUDED_CORP_IDS=98765432,98765433
```

Both accept comma-separated IDs. Omit or leave blank to disable. Entries added via `.env` are merged with those managed by `/exclusions` commands, which are persisted to `exclusions.json`.

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
├── bot.py               # Discord bot, slash commands, UI views, auto-post task
├── fetcher.py           # Data pipeline: zKillboard → ESI → enriched kill list
├── test_local.py        # Local test runner (no Discord required)
├── daily_config.json    # Persisted daily report config (auto-created on first /daily configure; gitignored)
├── exclusions.json      # Persisted exclusion list (auto-created on first /exclusions add; gitignored)
└── .env                 # DISCORD_TOKEN (not committed)
```

## How the fetch pipeline works

1. **Discover regions** — Queries ESI for all regions, samples one system per region to determine security status, and caches the resulting Null/Low/Wormhole/HighSec region lists for the lifetime of the process.
2. **Fetch matching kills** — For each selected ship group ID (or individual type ID for ships without a clean group) × selected regions, queries zKillboard's group-filter endpoint (`/api/groupID/{groupID}/regionID/{regionID}/...`) or ship-filter endpoint (`/api/shipID/{typeID}/...`). Requests are serialized (1 per second). Using group IDs reduces the typical industrial+mining query from ~2 100 requests to ~770 (~3× faster).
3. **Enrich** — Fetches the full ESI killmail for each matched kill; validates the victim's ship group against the selected categories (zKillboard filtering is best-effort, so mismatched kills are discarded here); filters out victims from excluded alliances or corporations; applies min-ISK and time-window filters; then resolves all remaining pilot, ship, corp, alliance, and solar system names in bulk via `/universe/names/`.
4. **Rate limiting** — Strictly 1 req/s to zKillboard (delay applied even on 404 responses); 100 ms between ESI requests; minimum 30 s backoff on HTTP 429 with up to 4 retries; ESI error budget monitored via `X-ESI-Error-Limit-Remain` (pauses 10 s if < 20 remaining).

## Time ranges

`15m` · `30m` · `1h` · `3h` · `6h` · `9h` · `12h` · `18h` · `24h` · `48h` · `7d`

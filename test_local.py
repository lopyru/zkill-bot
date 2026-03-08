"""
test_local.py — Run and inspect bot logic without Discord.
Tests the full fetch pipeline and prints what the embed would show.
"""

import asyncio
import json
from fetcher import fetch_all_kills

# ── Ship type ID → name lookup (mirrors INDUSTRIAL_SHIP_TYPE_IDS) ─────────────
SHIP_NAMES = {
    648: "Badger", 649: "Tayra", 650: "Iteron Mark V", 651: "Kryos",
    652: "Miasmos", 653: "Nereus", 654: "Wreathe", 655: "Hoarder",
    11466: "Crane", 11567: "Bustard", 12729: "Impel", 12731: "Mastodon",
    13477: "Prorator", 13479: "Viator", 12733: "Occator", 12735: "Bestower (DST)",
    20183: "Charon", 20185: "Providence", 20187: "Obelisk", 20189: "Fenrir",
    28846: "Rhea", 28848: "Nomad", 28850: "Anshar", 28852: "Ark",
    33697: "Porpoise", 28606: "Orca", 28352: "Rorqual",
}

# ── Mirrors build_summary_embed() from bot.py ────────────────────────────────

def print_summary(kills: list[dict]):
    total_value = sum(k.get("total_value", 0) for k in kills)
    total_value_str = f"{total_value / 1_000_000_000:.2f}B ISK" if total_value else "N/A"

    print("\n" + "=" * 60)
    print("  💀 NullSec + LowSec Industrial Kills — Last 24h")
    print("=" * 60)
    print(f"  Total Kills        : {len(kills):,}")
    print(f"  Total ISK Destroyed: {total_value_str}")
    print("=" * 60)

    if kills:
        print(f"\n  {'Pilot':<30} {'Ship':<22} {'Value':>14}  URL")
        print(f"  {'-'*30} {'-'*22} {'-'*14}  {'-'*40}")
        for k in kills[:20]:
            ship = SHIP_NAMES.get(k.get("ship_type_id", 0), f"TypeID {k.get('ship_type_id')}")
            value = f"{k.get('total_value', 0) / 1_000_000:.1f}M ISK"
            pilot = k.get("pilot_name", "Unknown")
            url   = k.get("zkill_url", "")
            print(f"  {pilot:<30} {ship:<22} {value:>14}  {url}")

        if len(kills) > 20:
            print(f"\n  ... and {len(kills) - 20} more (see killmails.json)")

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("Running local test — no Discord connection needed.")
    print("Fetching industrial kills from zKillboard + ESI...\n")

    kills = await fetch_all_kills()

    print_summary(kills)

    out = "killmails.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(kills, f, indent=2)
    print(f"\n  Full data saved to {out}")

if __name__ == "__main__":
    asyncio.run(main())
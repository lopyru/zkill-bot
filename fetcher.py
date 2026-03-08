"""
fetcher.py — zKillboard NullSec/LowSec kill fetcher
Async, using httpx. Accepts dynamic ship type sets and time ranges.
"""

import asyncio
import json
import httpx

# ── Config ────────────────────────────────────────────────────────────────────
USER_AGENT    = "zkill-nulllow-fetcher/1.0 maintainer@example.com"
REQUEST_DELAY = 1.0   # seconds between zKill requests — be polite
ESI_DELAY     = 0.5    # 500ms — conservative; ESI error budget monitored at runtime
MAX_PAGE          = 10    # safety cap per region
ZKILL_CONCURRENCY = 1     # serialize all zKill requests — their limit is ~1 req/s

ZKILL_BASE = "https://zkillboard.com/api"
ESI_BASE   = "https://esi.evetech.net/latest"

# Module-level cache so NullSec/LowSec region IDs are only discovered once per process
_nulllow_regions_cache: list[int] | None = None

HEADERS = {
    "Accept-Encoding": "gzip",
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}

# ── Ship category definitions ─────────────────────────────────────────────────
# Each category maps to a set of EVE ship type IDs.
# Source: https://www.fuzzwork.co.uk/dump/latest/invTypes.csv

SHIP_CATEGORIES: dict[str, dict] = {
    "industrial": {
        "label": "Industrial & Hauling",
        "emoji": "📦",
        "type_ids": {
            648, 649, 650, 651, 652, 653, 654, 655,   # T1 Industrials
            11466, 11567, 12729, 12731, 13477, 13479,  # Blockade Runners
            12733, 12735,                               # Deep Space Transports
            20183, 20185, 20187, 20189,                 # Freighters
            28846, 28848, 28850, 28852,                 # Jump Freighters
        },
    },
    "mining": {
        "label": "Mining",
        "emoji": "⛏️",
        "type_ids": {
            17476, 17478, 17480,                        # Mining Barges: Covetor, Retriever, Procurer
            22544, 22548, 22546,                        # Exhumers: Hulk, Mackinaw, Skiff
            32880, 33697, 37135, 89649, 89647,          # Mining Frigates: Venture, Prospect, Endurance, Outrider, Pioneer Consortium Issue
            42244, 28606, 28352,                        # Industrial Command: Porpoise, Orca, Rorqual
            2998,                                       # Salvager: Noctis
        },
    },
    "exploration": {
        "label": "Exploration",
        "emoji": "🔭",
        "type_ids": {
            11188, 11192, 11182, 11172, 11184, 11186,  # Covert Ops frigates
            29984, 29986, 29988, 29990,                 # T3 Cruisers (Legion, Proteus, Tengu, Loki)
            11174, 11176, 11178, 11196,                 # Expedition Frigates (Astero equiv class)
            37482,                                      # Astero
            35779,                                      # Stratios
        },
    },
    "capital": {
        "label": "Capital Ships",
        "emoji": "🚀",
        "type_ids": {
            # Carriers
            23757, 24483, 23911, 22852,
            # Dreadnoughts
            19720, 19722, 19724, 19726,
            # Force Auxiliaries
            37604, 37605, 37606, 37607,
            # Supercarriers
            3514, 23919, 23917, 42126,
            # Titans
            671, 11567, 23773, 45649,
        },
    },
    "pvp_cruiser": {
        "label": "PvP Cruisers & BCs",
        "emoji": "⚔️",
        "type_ids": {
            # Heavy Assault Cruisers
            12003, 12005, 12017, 12019, 11993, 11999, 12009, 12011,
            # Command Ships / Battlecruisers
            22442, 22448, 22474, 22466, 22452, 22456, 22460, 22464,
            # Recon Ships
            11957, 11963, 11959, 11961, 11971, 11965, 11969, 11975,
        },
    },
}

# ── Time range options ────────────────────────────────────────────────────────
TIME_RANGES: dict[str, dict] = {
    "1h":  {"label": "Last 1 hour",   "seconds": 3600},
    "6h":  {"label": "Last 6 hours",  "seconds": 21600},
    "12h": {"label": "Last 12 hours", "seconds": 43200},
    "24h": {"label": "Last 24 hours", "seconds": 86400},
    "48h": {"label": "Last 48 hours", "seconds": 172800},
    "7d":  {"label": "Last 7 days",   "seconds": 604800},
}

# ── Helpers ───────────────────────────────────────────────────────────────────

async def fetch_json(client: httpx.AsyncClient, url: str, delay: float = 0.0, _retries: int = 4):
    for attempt in range(_retries):
        try:
            resp = await client.get(url, timeout=30)
            if resp.status_code == 429:
                retry_after = max(30.0, float(resp.headers.get("Retry-After", 60)))
                print(f"  429 rate-limited — sleeping {retry_after:.0f}s (attempt {attempt + 1}/{_retries})")
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            # Watch ESI error budget — back off hard if running low
            error_remaining = int(resp.headers.get("X-ESI-Error-Limit-Remain", 100))
            if error_remaining < 20:
                print(f"  ⚠ ESI error budget low ({error_remaining} remaining) — pausing 10s")
                await asyncio.sleep(10)
            if delay:
                await asyncio.sleep(delay)
            return resp.json()
        except httpx.HTTPStatusError as e:
            print(f"  HTTP {e.response.status_code} -> {url}")
            return None
        except Exception as e:
            print(f"  Error fetching {url}: {e}")
            return None
    print(f"  Giving up after {_retries} attempts: {url}")
    return None

# ── Step 1: Discover NullSec / LowSec regions ────────────────────────────────

async def get_nulllow_regions(client: httpx.AsyncClient) -> list[int]:
    global _nulllow_regions_cache
    if _nulllow_regions_cache is not None:
        print(f"Using cached region list ({len(_nulllow_regions_cache)} NullSec/LowSec regions).")
        return _nulllow_regions_cache

    print("Fetching region list from ESI...")
    all_regions = await fetch_json(client, f"{ESI_BASE}/universe/regions/?datasource=tranquility")
    if not all_regions:
        raise RuntimeError("Could not fetch region list from ESI.")

    kspace = [r for r in all_regions if r < 11000000]
    print(f"  {len(kspace)} k-space regions to check.")
    nulllow = []

    for region_id in kspace:
        region_data = await fetch_json(
            client,
            f"{ESI_BASE}/universe/regions/{region_id}/?datasource=tranquility",
            delay=ESI_DELAY,
        )
        if not region_data:
            continue

        constellation_ids = region_data.get("constellations", [])
        if not constellation_ids:
            continue

        const_data = await fetch_json(
            client,
            f"{ESI_BASE}/universe/constellations/{constellation_ids[0]}/?datasource=tranquility",
            delay=ESI_DELAY,
        )
        if not const_data or not const_data.get("systems"):
            continue

        sys_data = await fetch_json(
            client,
            f"{ESI_BASE}/universe/systems/{const_data['systems'][0]}/?datasource=tranquility",
            delay=ESI_DELAY,
        )
        if not sys_data:
            continue

        sec = sys_data.get("security_status", 1.0)
        if sec < 0.45:
            sec_type = "NullSec" if sec < 0.0 else "LowSec "
            print(f"  + [{sec_type}] {region_data.get('name', region_id)} (sec={sec:.2f})")
            nulllow.append(region_id)

    print(f"\n  {len(nulllow)} NullSec/LowSec regions found.")
    _nulllow_regions_cache = nulllow
    return nulllow

# ── Step 2: Fetch kills per region ───────────────────────────────────────────

async def fetch_region_kills(
    client: httpx.AsyncClient, region_id: int, past_seconds: int
) -> list[dict]:
    all_kills = []
    page = 1

    while page <= MAX_PAGE:
        url = f"{ZKILL_BASE}/regionID/{region_id}/pastSeconds/{past_seconds}/page/{page}/"
        data = await fetch_json(client, url, delay=REQUEST_DELAY)

        if not data:
            break

        all_kills.extend(data)
        print(f"    Page {page}: +{len(data)} kills")

        if len(data) < 100:
            break

        page += 1

    return all_kills

# ── Step 2b: Fetch kills for one ship type + region (ship-filter path) ────────

async def _fetch_ship_region(
    client: httpx.AsyncClient,
    type_id: int,
    region_id: int,
    past_seconds: int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, list[dict]]:
    """Returns (type_id, kills) using zkill's built-in ship-type filter."""
    async with semaphore:
        kills = []
        page  = 1
        while page <= MAX_PAGE:
            url  = f"{ZKILL_BASE}/ship/{type_id}/regionID/{region_id}/pastSeconds/{past_seconds}/page/{page}/"
            data = await fetch_json(client, url, delay=0)  # delay handled below
            await asyncio.sleep(REQUEST_DELAY)              # always delay — even on 404/error
            if not data:
                break
            kills.extend(data)
            if len(data) < 100:
                break
            page += 1
        return type_id, kills

# ── Step 3: Fetch full killmail + resolve pilot name ─────────────────────────

async def fetch_full_killmail(
    client: httpx.AsyncClient, killmail_id: int, hash: str
) -> dict | None:
    url = f"{ESI_BASE}/killmails/{killmail_id}/{hash}/?datasource=tranquility"
    return await fetch_json(client, url, delay=ESI_DELAY)


async def resolve_character_name(client: httpx.AsyncClient, character_id: int) -> str:
    if not character_id:
        return "Unknown (NPC)"
    data = await fetch_json(
        client,
        f"{ESI_BASE}/characters/{character_id}/?datasource=tranquility",
        delay=ESI_DELAY,
    )
    return data.get("name", "Unknown") if data else "Unknown"


async def resolve_names_bulk(
    client: httpx.AsyncClient, character_ids: list[int]
) -> dict[int, str]:
    """Resolve up to 1 000 character IDs in a single ESI POST call."""
    if not character_ids:
        return {}

    name_map: dict[int, str] = {}
    for i in range(0, len(character_ids), 1000):
        batch = character_ids[i : i + 1000]
        for _ in range(4):
            try:
                resp = await client.post(
                    f"{ESI_BASE}/universe/names/?datasource=tranquility",
                    json=batch,
                    timeout=30,
                )
                if resp.status_code == 429:
                    wait = float(resp.headers.get("Retry-After", 60))
                    print(f"  429 on /universe/names/ — sleeping {wait:.0f}s")
                    await asyncio.sleep(wait)
                    continue
                if resp.status_code == 200:
                    for entry in resp.json():
                        if entry.get("category") == "character":
                            name_map[entry["id"]] = entry["name"]
                break
            except Exception as e:
                print(f"  Error bulk-resolving names: {e}")
                break
        if i + 1000 < len(character_ids):
            await asyncio.sleep(ESI_DELAY)

    return name_map

# ── Step 4: Filter + enrich ───────────────────────────────────────────────────

async def enrich_kills(
    client: httpx.AsyncClient,
    raw_kills: list[dict],
    target_type_ids: set[int],
) -> list[dict]:
    """
    Fetches full killmails from ESI, filters by target_type_ids,
    and resolves pilot names for matching kills only.
    """
    enriched = []

    for kill in raw_kills:
        killmail_id = kill.get("killmail_id")
        zkb         = kill.get("zkb", {})
        hash        = zkb.get("hash")

        if not killmail_id or not hash:
            continue

        full = await fetch_full_killmail(client, killmail_id, hash)
        if not full:
            continue

        victim       = full.get("victim", {})
        ship_type_id = victim.get("ship_type_id", 0)

        if ship_type_id not in target_type_ids:
            continue

        character_id = victim.get("character_id")
        pilot_name   = await resolve_character_name(client, character_id)

        enriched.append({
            "killmail_id":     killmail_id,
            "hash":            hash,
            "ship_type_id":    ship_type_id,
            "pilot_name":      pilot_name,
            "character_id":    character_id,
            "solar_system_id": full.get("solar_system_id"),
            "killmail_time":   full.get("killmail_time"),
            "total_value":     zkb.get("totalValue", 0),
            "points":          zkb.get("points", 0),
            "zkill_url":       f"https://zkillboard.com/kill/{killmail_id}/",
        })

        print(f"  ✓ {pilot_name} | ship {ship_type_id} | "
              f"{zkb.get('totalValue', 0) / 1_000_000:.1f}M ISK")

    return enriched

# ── Main fetch orchestrator ───────────────────────────────────────────────────

async def fetch_all_kills(
    category_keys: list[str] | None = None,
    past_seconds: int = 86400,
    on_progress=None,           # optional async callable: on_progress(dict) -> None
) -> list[dict]:
    """
    Full pipeline with optional filters.

    Args:
        category_keys: list of keys from SHIP_CATEGORIES to include.
                       Pass None or empty list to match ALL ships.
        past_seconds:  time window in seconds (must be multiple of 3600, max 604800).
    """
    # Build the combined type ID set from selected categories
    if category_keys:
        target_ids: set[int] = set()
        for key in category_keys:
            cat = SHIP_CATEGORIES.get(key)
            if cat:
                target_ids |= cat["type_ids"]
    else:
        target_ids = set()  # empty = no filter (all ships)

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        regions = await get_nulllow_regions(client)

        if not target_ids:
            # ── Unfiltered path: fetch all kills per region ───────────────────
            raw_killmails: list[dict] = []
            seen_ids: set[int] = set()

            for i, region_id in enumerate(regions, 1):
                print(f"\n[{i}/{len(regions)}] Fetching regionID {region_id}...")
                kills = await fetch_region_kills(client, region_id, past_seconds)
                new = [k for k in kills if k.get("killmail_id") not in seen_ids]
                seen_ids.update(k["killmail_id"] for k in new if "killmail_id" in k)
                raw_killmails.extend(new)
                print(f"  -> {len(new)} new (running total: {len(raw_killmails)})")

            print(f"\nNo ship filter applied — returning all {len(raw_killmails)} kills.")
            return raw_killmails

        # ── Filtered path: zkill ship-type filter → ESI killmail → bulk names ─
        if on_progress:
            await on_progress({"phase": "regions", "count": len(regions)})

        print(f"\nFetching {len(target_ids)} ship type(s) across {len(regions)} regions in parallel...")
        if on_progress:
            await on_progress({"phase": "zkill_start", "types": len(target_ids), "regions": len(regions)})

        semaphore   = asyncio.Semaphore(ZKILL_CONCURRENCY)
        tasks       = [
            _fetch_ship_region(client, type_id, region_id, past_seconds, semaphore)
            for type_id in target_ids
            for region_id in regions
        ]
        total_tasks = len(tasks)
        done_count  = 0

        # Use as_completed so we can report progress as queries finish
        seen_ids:    set[int]  = set()
        matched_raw: list[dict] = []
        for coro in asyncio.as_completed(tasks):
            type_id, kills = await coro
            done_count += 1
            for k in kills:
                kid = k.get("killmail_id")
                if kid and kid not in seen_ids:
                    seen_ids.add(kid)
                    k["_ship_type_id"] = type_id
                    matched_raw.append(k)
            if on_progress and (done_count % 20 == 0 or done_count == total_tasks):
                await on_progress({
                    "phase": "zkill_progress",
                    "done":  done_count,
                    "total": total_tasks,
                    "found": len(matched_raw),
                })

        print(f"  {len(matched_raw)} matching kills from zKillboard.")
        if on_progress:
            await on_progress({"phase": "zkill_done", "found": len(matched_raw)})

        # Fetch ESI killmails only for the matched kills (far fewer than before)
        print(f"  Fetching ESI killmails for {len(matched_raw)} kills...")
        enriched: list[dict] = []
        char_ids: list[int] = []

        for i, k in enumerate(matched_raw):
            killmail_id = k.get("killmail_id")
            zkb         = k.get("zkb", {})
            km_hash     = zkb.get("hash")
            if not killmail_id or not km_hash:
                continue

            full = await fetch_full_killmail(client, killmail_id, km_hash)
            if not full:
                continue

            victim       = full.get("victim", {})
            character_id = victim.get("character_id")
            char_ids.append(character_id or 0)

            enriched.append({
                "killmail_id":     killmail_id,
                "hash":            km_hash,
                "ship_type_id":    k["_ship_type_id"],
                "character_id":    character_id,
                "solar_system_id": full.get("solar_system_id"),
                "killmail_time":   full.get("killmail_time"),
                "total_value":     zkb.get("totalValue", 0),
                "points":          zkb.get("points", 0),
                "zkill_url":       f"https://zkillboard.com/kill/{killmail_id}/",
                "pilot_name":      None,  # filled in below
            })

            if on_progress and i % 5 == 0:
                await on_progress({"phase": "esi", "done": i + 1, "total": len(matched_raw)})

        # Resolve all pilot names in one bulk POST instead of N individual calls
        print(f"  Bulk-resolving {len(set(char_ids) - {0})} character names...")
        if on_progress:
            await on_progress({"phase": "names"})

        name_map = await resolve_names_bulk(
            client, [cid for cid in char_ids if cid]
        )

        for entry, char_id in zip(enriched, char_ids):
            entry["pilot_name"] = name_map.get(char_id, "Unknown (NPC)" if not char_id else "Unknown")
            print(f"  ✓ {entry['pilot_name']} | ship {entry['ship_type_id']} | "
                  f"{entry['total_value'] / 1_000_000:.1f}M ISK")

        print(f"\n  {len(enriched)} kills enriched.")
        return enriched


# ── Standalone test entry point ───────────────────────────────────────────────

if __name__ == "__main__":
    async def main():
        print("=" * 60)
        print("Standalone fetch test")
        print("=" * 60)
        # Example: industrial + mining ships, last 6 hours
        kills = await fetch_all_kills(
            category_keys=["industrial", "mining"],
            past_seconds=21600,
        )
        out = "killmails.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(kills, f, indent=2)
        print(f"\nDone. {len(kills)} killmails saved to {out}")

    asyncio.run(main())
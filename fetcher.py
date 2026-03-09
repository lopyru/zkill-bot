"""
fetcher.py — zKillboard NullSec/LowSec kill fetcher
Async, using httpx. Accepts dynamic ship type sets and time ranges.
"""

import asyncio
import json
import os
import httpx
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
USER_AGENT    = "zkill-nulllow-fetcher/1.0 maintainer@example.com"
REQUEST_DELAY = 1.0   # seconds between zKill requests — be polite
ESI_DELAY     = 0.1    # 100ms = 10 req/s — well under ESI's 20 req/s limit; error budget monitored at runtime
MAX_PAGE          = 10    # safety cap per region
ZKILL_CONCURRENCY = 1     # serialize all zKill requests — their limit is ~1 req/s

ZKILL_BASE = "https://zkillboard.com/api"
ESI_BASE   = "https://esi.evetech.net/latest"

# Alliance / corp IDs whose members should be excluded from results (e.g. your own)
# Set as comma-separated lists in .env, e.g.: EXCLUDED_ALLIANCE_IDS=99014523,99005065
EXCLUDED_ALLIANCE_IDS: set[int] = {
    int(x) for x in os.getenv("EXCLUDED_ALLIANCE_IDS", "").split(",") if x.strip()
}
EXCLUDED_CORP_IDS: set[int] = {
    int(x) for x in os.getenv("EXCLUDED_CORP_IDS", "").split(",") if x.strip()
}

# Module-level cache so region IDs are only discovered once per process
# Keys: "nullsec", "lowsec", "wormhole"
_region_cache: dict[str, list[int]] | None = None

# Cache ship type_id → group_id so repeated scans don't re-fetch the same types
_type_group_cache: dict[int, int] = {}

# Cache region name (lowercase) → region_id for the region filter feature
_region_name_to_id: dict[str, int] = {}

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
        "group_ids": {
            28,    # Hauler               — T1 Industrials (Badger, Mammoth, Iteron Mk V …)
            380,   # Deep Space Transport — Impel, Bustard, Occator, Mastodon
            1202,  # Blockade Runner      — Crane, Prorator, Viator, Prowler (+Meridian 81046)
            513,   # Freighter            — Charon, Providence, Obelisk, Fenrir (+faction)
            902,   # Jump Freighter       — Rhea, Ark, Anshar, Nomad
        },
        "type_ids": set(),
    },
    "mining": {
        "label": "Mining",
        "emoji": "⛏️",
        "group_ids": {
            463,   # Mining Barge             — Covetor, Retriever, Procurer
            543,   # Exhumer                  — Hulk, Mackinaw, Skiff
            1283,  # Expedition Frigate        — Prospect, Endurance (NOT Venture)
            941,   # Industrial Command Ship  — Porpoise, Orca
            883,   # Capital Industrial Ship  — Rorqual
        },
        "type_ids": {
            32880,  # Venture      (group 25 = Mining Frigates — too broad)
            89649,  # Outrider     (no dedicated group)
            89647,  # Pioneer Consortium Issue (no dedicated group)
            2998,   # Noctis       (salvager; group 28 covers it only if industrial also selected)
        },
    },
    "exploration": {
        "label": "Exploration",
        "emoji": "🔭",
        "group_ids": {
            830,   # Covert Ops Frigate — Buzzard, Anathema, Helios, Cheetah
            963,   # Strategic Cruiser  — Tengu, Legion, Proteus, Loki
        },
        "type_ids": {
            37482,                      # Astero
            35779,                      # Stratios
            11174, 11176, 11178, 11196, # Expedition Frigates
        },
    },
    "capital": {
        "label": "Capital Ships",
        "emoji": "🚀",
        "group_ids": {
            547,   # Carrier
            485,   # Dreadnought
            1538,  # Force Auxiliary
            659,   # Supercarrier
            30,    # Titan
        },
        "type_ids": set(),
    },
    "pvp_cruiser": {
        "label": "PvP Cruisers & BCs",
        "emoji": "⚔️",
        "group_ids": {
            358,   # Heavy Assault Cruiser
            540,   # Command Ship
            833,   # Force Recon Ship
            906,   # Combat Recon Ship
        },
        "type_ids": set(),
    },
}

# ── Time range options ────────────────────────────────────────────────────────
TIME_RANGES: dict[str, dict] = {
    "15m": {"label": "Last 15 minutes","seconds": 900},
    "30m": {"label": "Last 30 minutes","seconds": 1800},
    "1h":  {"label": "Last 1 hour",    "seconds": 3600},
    "6h":  {"label": "Last 6 hours",   "seconds": 21600},
    "12h": {"label": "Last 12 hours",  "seconds": 43200},
    "24h": {"label": "Last 24 hours",  "seconds": 86400},
    "48h": {"label": "Last 48 hours",  "seconds": 172800},
    "7d":  {"label": "Last 7 days",    "seconds": 604800},
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

# ── Step 1: Discover regions by security type ────────────────────────────────

async def get_regions(client: httpx.AsyncClient, on_log=None) -> dict[str, list[int]]:
    """Returns {"nullsec": [...], "lowsec": [...], "wormhole": [...], "highsec": [...]} cached for the process lifetime."""
    global _region_cache, _region_name_to_id
    if _region_cache is not None:
        msg = (f"Cached: {len(_region_cache['nullsec'])} NS · {len(_region_cache['lowsec'])} LS · "
               f"{len(_region_cache['wormhole'])} WH · {len(_region_cache['highsec'])} HS regions")
        print(msg)
        if on_log:
            await on_log(f"  {msg}")
        return _region_cache

    print("Fetching region list from ESI...")
    if on_log:
        await on_log("  Fetching region list from ESI...")
    all_regions = await fetch_json(client, f"{ESI_BASE}/universe/regions/?datasource=tranquility")
    if not all_regions:
        raise RuntimeError("Could not fetch region list from ESI.")

    wormhole = sorted(r for r in all_regions if r >= 11000000)
    kspace   = [r for r in all_regions if r < 11000000]
    print(f"  {len(kspace)} k-space regions to classify, {len(wormhole)} wormhole regions (by ID range).")

    nullsec: list[int] = []
    lowsec:  list[int] = []
    highsec: list[int] = []

    for region_id in kspace:
        region_data = await fetch_json(
            client,
            f"{ESI_BASE}/universe/regions/{region_id}/?datasource=tranquility",
            delay=ESI_DELAY,
        )
        if not region_data:
            continue

        region_name = region_data.get("name", "")
        if region_name:
            _region_name_to_id[region_name.lower()] = region_id

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
        if sec < 0.0:
            print(f"  + [NullSec] {region_name or region_id} (sec={sec:.2f})")
            nullsec.append(region_id)
        elif sec < 0.45:
            print(f"  + [LowSec ] {region_name or region_id} (sec={sec:.2f})")
            lowsec.append(region_id)
        else:
            highsec.append(region_id)   # ~27 regions; skip verbose print

    summary = f"  {len(nullsec)} NS · {len(lowsec)} LS · {len(wormhole)} WH · {len(highsec)} HS regions classified"
    print(f"\n{summary}")
    if on_log:
        await on_log(summary)
    _region_cache = {"nullsec": nullsec, "lowsec": lowsec, "wormhole": wormhole, "highsec": highsec}
    return _region_cache

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

        if len(data) < 200:
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
            url  = f"{ZKILL_BASE}/shipID/{type_id}/regionID/{region_id}/pastSeconds/{past_seconds}/page/{page}/"
            data = await fetch_json(client, url, delay=0)  # delay handled below
            await asyncio.sleep(REQUEST_DELAY)              # always delay — even on 404/error
            if not isinstance(data, list) or not data:
                break
            kills.extend(data)
            if len(data) < 200:
                break
            page += 1
        return type_id, kills

# ── Step 2c: Fetch kills for one group + region (group-filter path) ──────────

async def _fetch_group_region(
    client: httpx.AsyncClient,
    group_id: int,
    region_id: int,
    past_seconds: int,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """Returns kills using zkill's built-in group filter."""
    async with semaphore:
        kills = []
        page  = 1
        while page <= MAX_PAGE:
            url  = f"{ZKILL_BASE}/groupID/{group_id}/regionID/{region_id}/pastSeconds/{past_seconds}/page/{page}/"
            data = await fetch_json(client, url, delay=0)
            await asyncio.sleep(REQUEST_DELAY)
            if not isinstance(data, list) or not data:
                break
            kills.extend(data)
            if len(data) < 200:
                break
            page += 1
        return kills

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
    """
    Resolve character IDs to names.

    Tries ESI's bulk /universe/names/ POST first (up to 1 000 per batch).
    Falls back to individual /characters/{id}/ GET calls for any ID that
    the bulk endpoint didn't return (handles 404s from invalid/NPC IDs in batch).
    """
    if not character_ids:
        return {}

    unique_ids = list(dict.fromkeys(cid for cid in character_ids if cid))
    name_map: dict[int, str] = {}

    for i in range(0, len(unique_ids), 1000):
        batch = unique_ids[i : i + 1000]
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
                else:
                    print(f"  ⚠ ESI /universe/names/ HTTP {resp.status_code} "
                          f"for batch of {len(batch)} IDs: {resp.text[:200]}")
                break
            except Exception as e:
                print(f"  Error bulk-resolving names: {e}")
                break
        if i + 1000 < len(unique_ids):
            await asyncio.sleep(ESI_DELAY)

    # Fallback: individual lookups for any ID the bulk endpoint missed
    missing = [cid for cid in unique_ids if cid not in name_map]
    if missing:
        print(f"  Falling back to individual lookups for {len(missing)} unresolved IDs...")
        for cid in missing:
            name = await resolve_character_name(client, cid)
            if name not in ("Unknown", "Unknown (NPC)"):
                name_map[cid] = name
            await asyncio.sleep(ESI_DELAY)

    return name_map


async def resolve_type_names(client: httpx.AsyncClient, type_ids: list[int]) -> dict[int, str]:
    """Resolve EVE item type IDs to display names via /universe/names/."""
    if not type_ids:
        return {}
    name_map: dict[int, str] = {}
    try:
        resp = await client.post(
            f"{ESI_BASE}/universe/names/?datasource=tranquility",
            json=list(set(type_ids)),
            timeout=30,
        )
        if resp.status_code == 200:
            for entry in resp.json():
                if entry.get("category") == "inventory_type":
                    name_map[entry["id"]] = entry["name"]
        else:
            print(f"  ⚠ ESI /universe/names/ HTTP {resp.status_code} for type IDs")
    except Exception as e:
        print(f"  Error resolving type names: {e}")
    return name_map

async def get_type_group(client: httpx.AsyncClient, type_id: int) -> int | None:
    """Return the group_id for a ship type, using a process-lifetime cache."""
    if type_id in _type_group_cache:
        return _type_group_cache[type_id]
    data = await fetch_json(
        client,
        f"{ESI_BASE}/universe/types/{type_id}/?datasource=tranquility",
        delay=ESI_DELAY,
    )
    if data:
        group_id = data.get("group_id")
        if group_id:
            _type_group_cache[type_id] = group_id
            return group_id
    return None

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
    space_types: list[str] | None = None,  # subset of ["nullsec", "lowsec", "wormhole"]
    on_progress=None,                       # optional async callable: on_progress(dict) -> None
    on_log=None,                            # optional async callable: on_log(str) -> None
    stop_event=None,                        # asyncio.Event — set to abort with no results
    skip_event=None,                        # asyncio.Event — set to skip remaining scan and proceed to ESI
    min_isk: float | None = None,           # skip kills with totalValue below this threshold
    max_results: int | None = None,         # stop zKill scanning once this many raw kills are collected
    region_filter: list[str] | None = None, # restrict scan to specific region names (case-insensitive)
) -> list[dict]:
    """
    Full pipeline with optional filters.

    Args:
        category_keys: list of keys from SHIP_CATEGORIES to include.
                       Pass None or empty list to match ALL ships.
        past_seconds:  time window in seconds. Values < 3600 are supported via
                       client-side time filtering (zKillboard minimum is 1 hour).
        space_types:   which space to search. Defaults to ["nullsec", "lowsec"].
    """
    # zKillboard minimum time window is 1 hour; for shorter ranges we query 1h
    # and discard kills outside the actual window after ESI enrichment.
    zkill_seconds = max(past_seconds, 3600)
    cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=past_seconds)

    # Build group and type pairs from selected categories
    group_pairs: list[tuple[int, int]] = []
    type_pairs:  list[tuple[int, int]] = []

    if category_keys:
        for key in category_keys:
            cat = SHIP_CATEGORIES.get(key, {})
            for gid in cat.get("group_ids", set()):
                group_pairs.append((gid, 0))   # region added after discovery
            for tid in cat.get("type_ids", set()):
                type_pairs.append((tid, 0))

    selected_space = set(space_types) if space_types else {"nullsec", "lowsec"}

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        region_map = await get_regions(client, on_log=on_log)
        regions = [r for t in ("nullsec", "lowsec", "wormhole", "highsec") if t in selected_space for r in region_map.get(t, [])]

        # Apply optional region name filter
        if region_filter:
            filter_ids = {_region_name_to_id[n.lower()] for n in region_filter if n.lower() in _region_name_to_id}
            unknown    = [n for n in region_filter if n.lower() not in _region_name_to_id]
            if unknown:
                print(f"  ⚠ Unknown region name(s) ignored: {', '.join(unknown)}")
            if filter_ids:
                regions = [r for r in regions if r in filter_ids]
                print(f"  Region filter active: {len(regions)} region(s) selected ({', '.join(region_filter)})")
            else:
                print(f"  ⚠ Region filter matched nothing — scanning all selected space")

        # Reverse maps for tagging kills with category + space type
        group_to_cat    = {gid: key for key, cat in SHIP_CATEGORIES.items() for gid in cat.get("group_ids", set())}
        type_to_cat     = {tid: key for key, cat in SHIP_CATEGORIES.items() for tid in cat.get("type_ids", set())}
        region_to_space = {rid: t for t in ("nullsec", "lowsec", "wormhole", "highsec") for rid in region_map.get(t, [])}

        # Valid ship sets for ESI-side validation (filtered path only)
        valid_group_ids = {gid for key in (category_keys or []) for gid in SHIP_CATEGORIES.get(key, {}).get("group_ids", set())}
        valid_type_ids  = {tid for key in (category_keys or []) for tid in SHIP_CATEGORIES.get(key, {}).get("type_ids", set())}

        if not category_keys:
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

        # ── Filtered path: zkill group/ship filter → ESI killmail → bulk names ─
        if on_progress:
            await on_progress({"phase": "regions", "count": len(regions)})

        # Expand placeholder region (0) now that we have the real list
        group_pairs_full = [(gid, rid) for gid, _ in group_pairs for rid in regions]
        type_pairs_full  = [(tid, rid) for tid, _ in type_pairs  for rid in regions]
        total_tasks = len(group_pairs_full) + len(type_pairs_full)
        done_count  = 0

        n_groups = len({gid for gid, _ in group_pairs_full})
        n_types  = len({tid for tid, _ in type_pairs_full})
        print(f"\nFetching {n_groups} group(s) + {n_types} individual type(s) across {len(regions)} regions "
              f"({total_tasks} requests)...")
        if on_progress:
            await on_progress({"phase": "zkill_start", "types": total_tasks, "regions": len(regions)})

        semaphore    = asyncio.Semaphore(ZKILL_CONCURRENCY)
        seen_ids:    set[int]   = set()
        matched_raw: list[dict] = []

        # Sequential iteration — avoids background tasks outliving the httpx client
        for group_id, region_id in group_pairs_full:
            if stop_event and stop_event.is_set():
                break
            if skip_event and skip_event.is_set():
                break
            if max_results and len(matched_raw) >= max_results:
                print(f"  ⏹ Max results cap ({max_results}) reached — stopping zKill scan early.")
                break
            kills = await _fetch_group_region(client, group_id, region_id, zkill_seconds, semaphore)
            done_count += 1
            new_hits = sum(1 for k in kills if k.get("killmail_id") not in seen_ids)
            for k in kills:
                kid = k.get("killmail_id")
                if kid and kid not in seen_ids:
                    seen_ids.add(kid)
                    k["_category"]   = group_to_cat.get(group_id)
                    k["_space_type"] = region_to_space.get(region_id, "unknown")
                    matched_raw.append(k)
            if new_hits:
                print(f"  [{done_count}/{total_tasks}] groupID {group_id} / region {region_id} → {new_hits} kill(s)  (total: {len(matched_raw)})")
                if on_log:
                    await on_log(f"[{done_count}/{total_tasks}] groupID {group_id} / r{region_id} → {new_hits} kill(s)  (total: {len(matched_raw)})")
            elif done_count % 10 == 0:
                print(f"  [{done_count}/{total_tasks}] still scanning... ({len(matched_raw)} hits so far)")
                if on_log:
                    await on_log(f"  [{done_count}/{total_tasks}] scanning...  ({len(matched_raw)} hit(s) so far)")
            if on_progress and (done_count % 20 == 0 or done_count == total_tasks):
                await on_progress({
                    "phase": "zkill_progress",
                    "done":  done_count,
                    "total": total_tasks,
                    "found": len(matched_raw),
                })

        for type_id, region_id in type_pairs_full:
            if stop_event and stop_event.is_set():
                break
            if skip_event and skip_event.is_set():
                break
            if max_results and len(matched_raw) >= max_results:
                print(f"  ⏹ Max results cap ({max_results}) reached — stopping zKill scan early.")
                break
            _, kills = await _fetch_ship_region(client, type_id, region_id, zkill_seconds, semaphore)
            done_count += 1
            new_hits = 0
            for k in kills:
                kid = k.get("killmail_id")
                if kid and kid not in seen_ids:
                    seen_ids.add(kid)
                    k["_category"]   = type_to_cat.get(type_id)
                    k["_space_type"] = region_to_space.get(region_id, "unknown")
                    matched_raw.append(k)
                    new_hits += 1
            if new_hits:
                print(f"  [{done_count}/{total_tasks}] shipID {type_id} / region {region_id} → {new_hits} kill(s)  (total: {len(matched_raw)})")
                if on_log:
                    await on_log(f"[{done_count}/{total_tasks}] shipID {type_id} / r{region_id} → {new_hits} kill(s)  (total: {len(matched_raw)})")
            elif done_count % 10 == 0:
                print(f"  [{done_count}/{total_tasks}] still scanning... ({len(matched_raw)} hits so far)")
                if on_log:
                    await on_log(f"  [{done_count}/{total_tasks}] scanning...  ({len(matched_raw)} hit(s) so far)")
            if on_progress and (done_count % 20 == 0 or done_count == total_tasks):
                await on_progress({
                    "phase": "zkill_progress",
                    "done":  done_count,
                    "total": total_tasks,
                    "found": len(matched_raw),
                })

        if stop_event and stop_event.is_set():
            print("  ⏹ Stop requested — aborting, no results will be posted.")
            return []

        if skip_event and skip_event.is_set():
            print(f"  ⏭ Skip requested — proceeding with {len(matched_raw)} kills collected so far.")

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

            # Client-side time filter — needed when past_seconds < 3600
            km_time_str = full.get("killmail_time", "")
            if km_time_str:
                km_dt = datetime.fromisoformat(km_time_str.replace("Z", "+00:00"))
                if km_dt < cutoff_dt:
                    continue

            victim       = full.get("victim", {})
            if (victim.get("alliance_id") in EXCLUDED_ALLIANCE_IDS
                    or victim.get("corporation_id") in EXCLUDED_CORP_IDS):
                continue

            # ESI-side category validation: confirm victim's ship actually belongs
            # to the selected categories (zKillboard filtering is best-effort).
            if category_keys:
                ship_tid = victim.get("ship_type_id", 0)
                if ship_tid and ship_tid not in valid_type_ids:
                    ship_group = await get_type_group(client, ship_tid)
                    if ship_group not in valid_group_ids:
                        print(f"  ⚠ Kill {killmail_id}: ship {ship_tid} (group {ship_group}) not in selected categories — skipped")
                        continue

            total_value = zkb.get("totalValue", 0)
            if min_isk and total_value < min_isk:
                continue

            character_id = victim.get("character_id")
            char_ids.append(character_id or 0)

            isk_m = total_value / 1_000_000
            enriched.append({
                "killmail_id":     killmail_id,
                "hash":            km_hash,
                "ship_type_id":    victim.get("ship_type_id", 0),
                "character_id":    character_id,
                "solar_system_id": full.get("solar_system_id"),
                "killmail_time":   full.get("killmail_time"),
                "total_value":     total_value,
                "points":          zkb.get("points", 0),
                "zkill_url":       f"https://zkillboard.com/kill/{killmail_id}/",
                "pilot_name":      None,  # filled below
                "ship_name":       None,  # filled below
                "category":        k.get("_category"),
                "space_type":      k.get("_space_type"),
            })
            if on_log:
                ts = (full.get("killmail_time") or "")[:16].replace("T", " ")
                await on_log(f"  kill {killmail_id}  {isk_m:>8.0f}M ISK  {ts}")

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

        # Resolve ship type names
        ship_type_ids = list({e["ship_type_id"] for e in enriched if e.get("ship_type_id")})
        if ship_type_ids:
            print(f"  Resolving {len(ship_type_ids)} ship type name(s)...")
            type_name_map = await resolve_type_names(client, ship_type_ids)
            for entry in enriched:
                entry["ship_name"] = type_name_map.get(entry["ship_type_id"], "Unknown Ship")

        # Log resolved kills (pilot + ship now both available)
        if on_log:
            for entry in enriched:
                pilot = entry.get("pilot_name") or "Unknown"
                ship  = entry.get("ship_name")  or f"typeID {entry['ship_type_id']}"
                isk   = entry["total_value"] / 1_000_000
                await on_log(f"  ✓ {pilot} — {ship} — {isk:.0f}M ISK")

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
"""
bot.py — py-cord Discord bot with interactive filter form.
"""

import asyncio
import collections
import io
import json
import os
import time as _time
from datetime import datetime
import discord
from discord.ext import tasks
from dotenv import load_dotenv

from fetcher import (
    fetch_all_kills, SHIP_CATEGORIES, TIME_RANGES,
    EXCLUDED_ALLIANCE_IDS, EXCLUDED_CORP_IDS, search_entity_ids,
)

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DEV_GUILD_ID         = 671707585262649344   # e.g. 123456789012345678 — set for instant dev registration
AUTO_POST_CHANNEL_ID = 1480291942389514370   # e.g. 987654321098765432 — set to enable daily auto-post
AUTO_POST_CATEGORIES = ["industrial", "mining"]   # categories used by the daily auto-post
AUTO_POST_TIME_KEY   = "24h"                       # time range key used by the daily auto-post
MAX_SCAN_RESULTS: int | None = None                # cap zKillboard scanning early (e.g. 100); None = no cap
HIGHSEC_MAX_RESULTS: int = 200                     # automatic cap when High Security space is selected

# ── Daily config persistence ───────────────────────────────────────────────────
DAILY_CONFIG_PATH = "daily_config.json"

def _load_daily_config() -> dict:
    defaults: dict = {
        "enabled":    bool(AUTO_POST_CHANNEL_ID),
        "channel_id": AUTO_POST_CHANNEL_ID,
        "categories": list(AUTO_POST_CATEGORIES),
        "time_key":   AUTO_POST_TIME_KEY,
        "space_types": ["nullsec", "lowsec"],
        "min_isk":    None,
    }
    try:
        with open(DAILY_CONFIG_PATH) as f:
            defaults.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults

def _save_daily_config() -> None:
    with open(DAILY_CONFIG_PATH, "w") as f:
        json.dump(_daily_cfg, f, indent=2)

_daily_cfg: dict = _load_daily_config()

# ── Exclusions persistence ────────────────────────────────────────────────────
EXCLUSIONS_PATH = "exclusions.json"

def _load_exclusions() -> dict:
    defaults: dict = {"alliances": {}, "corporations": {}}
    try:
        with open(EXCLUSIONS_PATH) as f:
            defaults.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults

def _save_exclusions() -> None:
    with open(EXCLUSIONS_PATH, "w") as f:
        json.dump(_exclusions_cfg, f, indent=2)

_exclusions_cfg: dict = _load_exclusions()

# Merge JSON-persisted exclusions into the live fetcher sets
# (.env values are already in those sets from module initialisation)
for _aid in list(_exclusions_cfg.get("alliances", {})):
    EXCLUDED_ALLIANCE_IDS.add(int(_aid))
for _cid in list(_exclusions_cfg.get("corporations", {})):
    EXCLUDED_CORP_IDS.add(int(_cid))

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot     = discord.Bot(intents=intents)

fetch_in_progress = False
_skip_first_daily = True   # skip the immediate fire on startup; run after first 24h interval
_stop_event:    asyncio.Event | None = None   # set by /stop  — aborts scan, no results posted
_skip_event:    asyncio.Event | None = None   # set by /skip  — skips remaining scan, posts partial results
_fetch_phase:   str = ""                      # "zkill" | "esi" | "names" | ""
_fetch_start_ts: float | None = None          # monotonic timestamp when current fetch started
_last_embeds: collections.deque = collections.deque(maxlen=5)  # embeds from the last 5 completed scans

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"

def build_summary_embed(kills: list[dict], category_keys: list[str], time_key: str, elapsed: float | None = None) -> discord.Embed:
    total_value = sum(k.get("total_value", 0) for k in kills)
    if total_value >= 1e12:
        value_str = f"{total_value/1e12:.2f}T ISK"
    elif total_value >= 1e9:
        value_str = f"{total_value/1e9:.2f}B ISK"
    else:
        value_str = f"{total_value/1e6:.0f}M ISK"

    cat_names  = " · ".join(SHIP_CATEGORIES[k]["label"] for k in category_keys) if category_keys else "All ships"
    time_label = TIME_RANGES[time_key]["label"] if time_key in TIME_RANGES else time_key

    unique_pilots = len({k.get("pilot_name") for k in kills
                         if k.get("pilot_name") not in (None, "Unknown", "Unknown (NPC)")})

    _SPACE  = {"nullsec": "⚫ Null Sec", "lowsec": "🔴 Low Sec", "wormhole": "🌀 Wormhole", "highsec": "🟡 High Sec"}
    present = {k.get("space_type") for k in kills if k.get("space_type")}
    space_str = "  ·  ".join(v for s, v in _SPACE.items() if s in present)

    embed = discord.Embed(
        title="Kill Report  ✅",
        description=(
            f"**🚢 Ship categories:**\n{cat_names}\n"
            f"**⏱ Time Range:**\n{time_label}\n"
            f"**🌌 Security:**\n{space_str}\n"
            + (f"**⏳ Scan took:**\n{_fmt_elapsed(elapsed)}" if elapsed is not None else "")
        ),
        color=discord.Color.red(),
    )
    embed.add_field(name="💀 Kills",         value=f"**{len(kills):,}**",   inline=True)
    embed.add_field(name="👤 Unique pilots", value=f"**{unique_pilots:,}**", inline=True)
    embed.add_field(name="💰 Total ISK",     value=f"**{value_str}**",       inline=True)

    # Breakdown by category
    if category_keys:
        cat_lines = []
        for key in category_keys:
            cat_kills = [k for k in kills if k.get("category") == key]
            if cat_kills:
                isk   = sum(k.get("total_value", 0) for k in cat_kills)
                isk_s = f"{isk/1e9:.2f}B" if isk >= 1e9 else f"{isk/1e6:.0f}M"
                cat_lines.append(
                    f"{SHIP_CATEGORIES[key]['emoji']} {SHIP_CATEGORIES[key]['label']}: "
                    f"**{len(cat_kills)}** kills  ·  {isk_s} ISK"
                )
        if cat_lines:
            embed.add_field(name="📊 By category", value="\n".join(cat_lines), inline=False)

    # Breakdown by security
    space_lines = []
    for s_key in ("nullsec", "lowsec", "wormhole", "highsec"):
        s_kills = [k for k in kills if k.get("space_type") == s_key]
        if s_kills:
            emoji = {"nullsec": "⚫", "lowsec": "🔴", "wormhole": "🌀", "highsec": "🟡"}[s_key]
            label = {"nullsec": "Null Sec", "lowsec": "Low Sec", "wormhole": "Wormhole", "highsec": "High Sec"}[s_key]
            isk   = sum(k.get("total_value", 0) for k in s_kills)
            isk_s = f"{isk/1e9:.2f}B" if isk >= 1e9 else f"{isk/1e6:.0f}M"
            space_lines.append(f"{emoji} {label}: **{len(s_kills)}** kills  ·  {isk_s} ISK")
    if space_lines:
        embed.add_field(name="🌌 By security", value="\n".join(space_lines), inline=False)

    embed.set_footer(text="Data via zKillboard + ESI")
    return embed


async def post_kill_details(channel: discord.TextChannel, kills: list[dict]):
    """Post the detail list, pilot mail list, and EVE mail body to *channel*."""
    _SEC_EMOJI = {"nullsec": "⚫", "lowsec": "🔴", "wormhole": "🌀", "highsec": "🟡"}

    def _discord_ts(km_time: str) -> str:
        try:
            dt = datetime.fromisoformat(km_time.replace("Z", "+00:00"))
            return f"<t:{int(dt.timestamp())}:R>"
        except Exception:
            return km_time[:16].replace("T", " ") + " UTC"

    def _utc_ts(km_time: str) -> str:
        try:
            dt = datetime.fromisoformat(km_time.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            return km_time[:16].replace("T", " ") + " UTC"

    def _fmt_isk(value: float) -> str:
        if value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.2f}B ISK"
        return f"{value / 1_000_000:.0f}M ISK"

    detail_lines = []
    md_lines     = []
    for k in kills:
        pilot = k.get("pilot_name", "")
        if not pilot or pilot in ("Unknown", "Unknown (NPC)"):
            continue
        sec_e     = _SEC_EMOJI.get(k.get("space_type", ""), "")
        ship      = k.get("ship_name") or f"Ship {k.get('ship_type_id', '?')}"
        isk_str   = _fmt_isk(k.get("total_value", 0))
        url       = k.get("zkill_url", "")
        corp      = k.get("corp_name", "")
        system    = k.get("solar_system_name", "?")
        corp_part = f" ({corp})" if corp else ""
        detail_lines.append(f"{sec_e} {pilot} | {corp} | {ship} | {isk_str} | {system} | {_utc_ts(k.get('killmail_time', ''))} | {url}")
        md_lines.append(f"{sec_e} **{pilot}**{corp_part} — {ship} — {isk_str} — 📍 {system} — {_discord_ts(k.get('killmail_time', ''))} — 🔗 <{url}>")

    if md_lines:
        d_header = f"📋 **Kill details** ({len(md_lines)} kills):\n"
        sep      = "─" * 44
        md_block = "\n".join(md_lines)
        full_msg = f"{d_header}{sep}\n{md_block}\n{sep}"
        if len(full_msg) <= 1900:
            await channel.send(content=full_msg)
        else:
            buf = io.BytesIO("\n".join(detail_lines).encode("utf-8"))
            await channel.send(content=d_header, file=discord.File(buf, filename="kills.txt"))

    # ── Unique pilot list ─────────────────────────────────────────────────────
    seen_cids: set[int] = set()
    unique_pilots: list[dict] = []
    for k in kills:
        cid   = k.get("character_id")
        pilot = k.get("pilot_name", "")
        if not cid or not pilot or pilot in ("Unknown", "Unknown (NPC)"):
            continue
        if cid not in seen_cids:
            seen_cids.add(cid)
            unique_pilots.append({"name": pilot, "cid": cid})

    if unique_pilots:
        unique_names = [p["name"] for p in unique_pilots]
        comma_list   = ", ".join(unique_names)
        m_header     = f"📮 **In-game mail list** ({len(unique_names)} pilots):\n"
        if len(comma_list) <= 1800:
            await channel.send(content=f"{m_header}```\n{comma_list}\n```")
        else:
            buf = io.BytesIO(comma_list.encode("utf-8"))
            await channel.send(content=m_header, file=discord.File(buf, filename="pilot_names.txt"))

        eve_links = "".join(
            f'<a href="showinfo:1375//{p["cid"]}">{p["name"]}</a><br>'
            for p in unique_pilots
        )
        eve_body = f'<font size="14" color="#ffd98d00">{eve_links}</font>'
        e_header = f"📨 **EVE in-game mail — HTML pilot links** ({len(unique_pilots)} pilots):\n"
        if len(eve_body) <= 1800:
            await channel.send(content=f"{e_header}```\n{eve_body}\n```")
        else:
            buf = io.BytesIO(eve_body.encode("utf-8"))
            await channel.send(content=e_header, file=discord.File(buf, filename="eve_mail.txt"))


# ── Region filter modal ───────────────────────────────────────────────────────

class RegionModal(discord.ui.Modal):
    """Opened by the Regions button — lets the user type region names."""

    def __init__(self, view_ref, btn_interaction: discord.Interaction):
        super().__init__(title="📍 Filter by Region")
        self.view_ref       = view_ref
        self.btn_interaction = btn_interaction
        self.add_item(discord.ui.InputText(
            label="Region names (comma-separated, blank = all)",
            placeholder="e.g. Delve, Querious, The Bleak Lands",
            style=discord.InputTextStyle.short,
            required=False,
            max_length=200,
        ))

    async def callback(self, interaction: discord.Interaction):
        raw = self.children[0].value.strip()
        self.view_ref.selected_regions = [r.strip() for r in raw.split(",") if r.strip()] if raw else []
        await interaction.response.defer()
        await self.btn_interaction.edit_original_response(
            content=self.view_ref._form_content(), view=self.view_ref
        )


# ── Filter UI ─────────────────────────────────────────────────────────────────

class KillFilterView(discord.ui.View):
    """
    Ephemeral filter form sent when the user runs /kills.
    Presents a ship category multi-select, a time range select,
    and a Fetch button.
    """

    def __init__(self):
        super().__init__(timeout=120)  # form expires after 2 minutes
        self.selected_categories: list[str] = []
        self.selected_time_key: str         = "24h"
        self.selected_space:     list[str]  = ["nullsec", "lowsec"]
        self.selected_min_isk:   int | None = None
        self.selected_regions:   list[str]  = []

    def _form_content(self) -> str:
        cat_label = (
            ", ".join(SHIP_CATEGORIES[k]["label"] for k in self.selected_categories)
            or "*(required — select at least one)*"
        )
        time_label   = TIME_RANGES[self.selected_time_key]["label"]
        _space_names = {"nullsec": "Null Security", "lowsec": "Low Security", "wormhole": "Wormhole Space", "highsec": "High Security"}
        if self.selected_space:
            space_label = " + ".join(_space_names[s] for s in self.selected_space)
        elif self.selected_regions:
            space_label = "*(all — no security filter)*"
        else:
            space_label = "*(required — select at least one)*"
        isk_label    = f"{self.selected_min_isk // 1_000_000:,}M ISK" if self.selected_min_isk else "*(none)*"
        region_label = ", ".join(self.selected_regions) if self.selected_regions else "*(all regions in selected space)*"
        hs_warning   = (
            f"\n⚠️ High Security selected — results capped at **{HIGHSEC_MAX_RESULTS}** kills to keep scan time reasonable."
            if "highsec" in self.selected_space else ""
        )
        return (
            f"✅ Categories: **{cat_label}**\n"
            f"⏱ Time range: **{time_label}**\n"
            f"🌌 Space: **{space_label}**\n"
            f"💰 Min ISK: **{isk_label}**\n"
            f"🗺️ Regions: **{region_label}**"
            f"{hs_warning}\n\n"
            "Hit **Fetch** when ready."
        )

    # ── Ship category multi-select ────────────────────────────────────────────

    @discord.ui.select(
        placeholder="🚢  Choose ship categories…",
        min_values=1,
        max_values=len(SHIP_CATEGORIES),
        options=[
            discord.SelectOption(
                label=cat["label"],
                value=key,
                emoji=cat["emoji"],
            )
            for key, cat in SHIP_CATEGORIES.items()
        ],
    )
    async def category_select(
        self, select: discord.ui.Select, interaction: discord.Interaction
    ):
        self.selected_categories = select.values
        # Mark selected options so they persist visually after re-render
        for option in select.options:
            option.default = option.value in self.selected_categories

        await interaction.response.edit_message(content=self._form_content(), view=self)

    # ── Time range single-select ──────────────────────────────────────────────

    @discord.ui.select(
        placeholder="⏱  Time range…",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label=info["label"],
                value=key,
                default=(key == "24h"),
            )
            for key, info in TIME_RANGES.items()
        ],
    )
    async def time_select(
        self, select: discord.ui.Select, interaction: discord.Interaction
    ):
        self.selected_time_key = select.values[0]
        # Mark selected option so it persists visually after re-render
        for option in select.options:
            option.default = option.value == self.selected_time_key

        await interaction.response.edit_message(content=self._form_content(), view=self)

    # ── Space type multi-select ───────────────────────────────────────────────

    @discord.ui.select(
        placeholder="🌌  Space type…",
        min_values=0,
        max_values=4,
        options=[
            discord.SelectOption(label="Null Security",  value="nullsec",  emoji="⚫", default=True),
            discord.SelectOption(label="Low Security",   value="lowsec",   emoji="🔴", default=True),
            discord.SelectOption(label="Wormhole Space", value="wormhole", emoji="🌀", default=False),
            discord.SelectOption(label="High Security",  value="highsec",  emoji="🟡", default=False),
        ],
    )
    async def space_select(
        self, select: discord.ui.Select, interaction: discord.Interaction
    ):
        self.selected_space = select.values
        for option in select.options:
            option.default = option.value in self.selected_space

        await interaction.response.edit_message(content=self._form_content(), view=self)

    # ── Min ISK filter single-select ──────────────────────────────────────────

    @discord.ui.select(
        placeholder="💰  Min ISK value… (optional)",
        min_values=0,
        max_values=1,
        options=[
            discord.SelectOption(label="10M ISK minimum",  value="10000000"),
            discord.SelectOption(label="50M ISK minimum",  value="50000000"),
            discord.SelectOption(label="100M ISK minimum", value="100000000"),
            discord.SelectOption(label="250M ISK minimum", value="250000000"),
            discord.SelectOption(label="500M ISK minimum", value="500000000"),
            discord.SelectOption(label="1B ISK minimum",   value="1000000000"),
        ],
    )
    async def min_isk_select(
        self, select: discord.ui.Select, interaction: discord.Interaction
    ):
        self.selected_min_isk = int(select.values[0]) if select.values else None
        for option in select.options:
            option.default = bool(select.values) and option.value == select.values[0]
        await interaction.response.edit_message(content=self._form_content(), view=self)

    # ── Fetch button ──────────────────────────────────────────────────────────

    @discord.ui.button(label="Fetch", style=discord.ButtonStyle.danger, emoji="🔍")
    async def fetch_button(
        self, _button: discord.ui.Button, interaction: discord.Interaction
    ):
        global fetch_in_progress, _stop_event, _skip_event, _fetch_phase, _fetch_start_ts, _last_embeds

        if not self.selected_categories:
            await interaction.response.edit_message(
                content="⚠️ Please select at least one ship category.\n\n" + self._form_content(),
                view=self,
            )
            return

        if not self.selected_space and not self.selected_regions:
            await interaction.response.edit_message(
                content="⚠️ Please select at least one space type, or enter specific regions.\n\n" + self._form_content(),
                view=self,
            )
            return

        if fetch_in_progress:
            await interaction.response.edit_message(
                content=(
                    "⚠️ **A fetch is already in progress — please wait.**\n\n"
                    + self._form_content().replace("Hit **Fetch** when ready.", "Your selections are saved. Hit **Fetch** again when the scan completes.")
                ),
                view=self,
            )
            return

        fetch_in_progress = True
        _stop_event    = asyncio.Event()
        _skip_event    = asyncio.Event()
        _fetch_phase   = ""
        _fetch_start_ts = _time.monotonic()
        caller       = interaction.user
        channel      = interaction.channel
        time_key     = self.selected_time_key
        time_label   = TIME_RANGES[time_key]["label"]
        cat_label    = ", ".join(SHIP_CATEGORIES[k]["label"] for k in self.selected_categories)
        past_seconds = TIME_RANGES[time_key]["seconds"]
        categories     = self.selected_categories or None
        min_isk        = self.selected_min_isk
        region_filter  = self.selected_regions or None
        max_results    = (
            HIGHSEC_MAX_RESULTS if "highsec" in self.selected_space
            else MAX_SCAN_RESULTS
        )

        # ── Live progress state ───────────────────────────────────────────────
        W = 14  # progress bar width (chars)
        _SPACE_LABELS  = {"nullsec": "⚫ Null Sec", "lowsec": "🔴 Low Sec", "wormhole": "🌀 Wormhole", "highsec": "🟡 High Sec"}
        space_label    = " · ".join(_SPACE_LABELS[s] for s in self.selected_space if s in _SPACE_LABELS)
        isk_label      = f"{min_isk // 1_000_000:,}M ISK" if min_isk else None
        region_label   = ", ".join(region_filter) if region_filter else None
        stages = {
            "regions": "[ ] Classifying NS / LS / WH regions...",
            "zkill":   "[ ] Waiting for region list...",
            "esi":     "[ ] Pending kill data",
            "names":   "[ ] Pending ESI enrichment",
        }
        log_buffer  = collections.deque(["  ·", "  ·", "  ·"], maxlen=6)
        start_ts    = [_time.monotonic()]
        est_seconds = [0]
        _loop_stop  = [False]

        def _bar(done: int, total: int) -> str:
            if total == 0:
                return "─" * W
            filled = round(W * done / total)
            return "█" * filled + "░" * (W - filled)

        def _elapsed() -> str:
            s = int(_time.monotonic() - start_ts[0])
            m, s = divmod(s, 60)
            return f"{m}m {s:02d}s" if m else f"{s}s"

        def _fmt_est(s: int) -> str:
            if s <= 0:
                return "—"
            m, sec = divmod(s, 60)
            return f"~{m}m {sec:02d}s" if m else f"~{sec}s"

        caller_tag = f"{caller.display_name} ({caller.name})"

        def build_status() -> str:
            sep = "─" * 48
            eta = f"  (est. {_fmt_est(est_seconds[0])})" if est_seconds[0] else ""
            log_lines = "\n".join(log_buffer)
            isk_line    = f"Min ISK:     {isk_label}\n" if isk_label else ""
            region_line = f"Regions:     {region_label[:42]}\n" if region_label else ""
            return (
                f"Scan triggered by **{caller_tag}**\n"
                f"```\n"
                f"Categories:  {cat_label[:36]}\n"
                f"Time range:  {time_label}\n"
                f"Security:    {space_label}\n"
                f"{isk_line}"
                f"{region_line}"
                f"{sep}\n"
                f"Regions     {stages['regions']}\n"
                f"zKillboard  {stages['zkill']}\n"
                f"ESI         {stages['esi']}\n"
                f"Names       {stages['names']}\n"
                f"{sep}\n"
                f"{log_lines}\n"
                f"{sep}\n"
                f"Elapsed     {_elapsed()}{eta}\n"
                f"```"
            )

        # Fix #3: dismiss the ephemeral form, open a public progress panel
        await interaction.response.edit_message(
            content=f"🔍 Fetching… see progress in <#{channel.id}>", view=None
        )
        status_msg = await channel.send(content=build_status())

        def _term_status():
            """Overwrite the current terminal line with a compact live status."""
            zkill_s = (stages['zkill']
                       .replace("[ ]", " ").replace("[~]", "~").replace("[✓]", "✓"))
            line = f"  [{_elapsed()}]  {zkill_s}"
            print(f"\r\033[2K{line[:120]}", end="", flush=True)

        async def _edit_loop():
            while not _loop_stop[0]:
                await asyncio.sleep(0.5)
                try:
                    await status_msg.edit(content=build_status())
                except Exception:
                    pass
                _term_status()

        loop_task = asyncio.ensure_future(_edit_loop())

        async def on_progress(event: dict):
            global _fetch_phase
            phase = event.get("phase")
            if phase == "regions":
                stages["regions"] = f"[✓] {event['count']} regions classified"
                stages["zkill"]   = "[~] Scanning ship kills across all selected regions..."
            elif phase == "zkill_start":
                _fetch_phase = "zkill"
                total = event["types"]
                est_seconds[0] = total   # 1 req/s → total seconds
                stages["zkill"] = f"[~] {_bar(0, total)}  0/{total}  — querying zKillboard"
            elif phase == "zkill_progress":
                done, total, found = event["done"], event["total"], event["found"]
                stages["zkill"] = f"[~] {_bar(done, total)}  {done}/{total}  ({found} hits)"
            elif phase == "zkill_done":
                _fetch_phase = "esi"
                found = event["found"]
                stages["zkill"] = f"[✓] {found} kill{'s' if found != 1 else ''} matched"
                stages["esi"]   = f"[~] {_bar(0, found)}  0/{found}  — fetching ESI killmails"
            elif phase == "esi":
                done, total = event["done"], event["total"]
                stages["esi"] = f"[~] {_bar(done, total)}  {done}/{total}  — enriching kills"
            elif phase == "names":
                _fetch_phase = "names"
                stages["esi"]   = stages["esi"].replace("[~]", "[✓]")
                stages["names"] = "[~] Resolving pilot names & ship types..."

        async def on_log(line: str):
            log_buffer.append(line)

        try:
            kills = await asyncio.wait_for(
                fetch_all_kills(
                    category_keys=categories,
                    past_seconds=past_seconds,
                    space_types=self.selected_space,
                    on_progress=on_progress,
                    on_log=on_log,
                    stop_event=_stop_event,
                    skip_event=_skip_event,
                    min_isk=min_isk,
                    max_results=max_results,
                    region_filter=region_filter,
                ),
                timeout=3600,  # 1-hour hard cap — prevents indefinite hangs
            )
            if _stop_event is not None and _stop_event.is_set():
                await channel.send(f"⏹ **Scan stopped.** No results posted.")
                await status_msg.edit(content="⏹ Stopped.")
            elif not kills:
                _SPACE_NAMES = {"nullsec": "Null Sec", "lowsec": "Low Sec", "wormhole": "Wormhole Space", "highsec": "High Sec"}
                space_msg = " + ".join(_SPACE_NAMES[s] for s in self.selected_space if s in _SPACE_NAMES)
                await channel.send(
                    content=(
                        f"{caller.mention} — no kills found for **{cat_label}** "
                        f"in the **{time_label.lower()}** in **{space_msg}**."
                    )
                )
                await status_msg.edit(content="✅ Done — no results.")
            else:
                embed = build_summary_embed(kills, self.selected_categories, time_key,
                                            elapsed=_time.monotonic() - _fetch_start_ts)
                _last_embeds.appendleft(embed)
                await channel.send(content=f"{caller.mention} — kill report ready!", embed=embed)
                await status_msg.edit(content="✅ Done!")
                try:
                    await post_kill_details(channel, kills)
                except Exception as e:
                    await channel.send(f"⚠️ Kill details could not be posted: `{type(e).__name__}: {e}`")
        except asyncio.TimeoutError:
            await channel.send(f"{caller.mention} ⏱ **Scan timed out** after 1 hour — no results posted.")
            await status_msg.edit(content="⏱ Timed out.")
        except Exception as e:
            await status_msg.edit(content=f"❌ Fetch failed: `{type(e).__name__}: {e}`")
        finally:
            _loop_stop[0] = True
            loop_task.cancel()
            print()  # move terminal cursor past the live status line
            _stop_event     = None
            _skip_event     = None
            _fetch_phase    = ""
            _fetch_start_ts = None
            fetch_in_progress = False

    # ── Region filter button ──────────────────────────────────────────────────

    @discord.ui.button(label="Regions", style=discord.ButtonStyle.secondary, emoji="🗺️")
    async def region_button(
        self, _button: discord.ui.Button, interaction: discord.Interaction
    ):
        await interaction.response.send_modal(RegionModal(view_ref=self, btn_interaction=interaction))

    # ── Cancel button ─────────────────────────────────────────────────────────

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ):
        self.disable_all_items()
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

    async def on_timeout(self):
        self.disable_all_items()
        self.stop()

# ── Daily config UI ───────────────────────────────────────────────────────────

class DailyConfigView(discord.ui.View):
    """Ephemeral form for /daily configure — mirrors KillFilterView but saves to _daily_cfg."""

    def __init__(self):
        super().__init__(timeout=120)
        self.selected_categories: list[str] = list(_daily_cfg["categories"])
        self.selected_time_key:   str        = _daily_cfg["time_key"]
        self.selected_space:      list[str]  = list(_daily_cfg["space_types"])
        self.selected_min_isk:    int | None = _daily_cfg.get("min_isk")
        # Pre-fill dropdown visuals from current config (selects are in declaration order)
        selects = [c for c in self.children if isinstance(c, discord.ui.Select)]
        for opt in selects[0].options:  # categories
            opt.default = opt.value in self.selected_categories
        for opt in selects[1].options:  # time range
            opt.default = opt.value == self.selected_time_key
        for opt in selects[2].options:  # space
            opt.default = opt.value in self.selected_space
        if self.selected_min_isk:
            for opt in selects[3].options:  # min ISK
                opt.default = opt.value == str(self.selected_min_isk)

    def _form_content(self) -> str:
        cat_label  = ", ".join(SHIP_CATEGORIES[k]["label"] for k in self.selected_categories) or "*(none)*"
        time_label = TIME_RANGES[self.selected_time_key]["label"]
        _snames    = {"nullsec": "Null Security", "lowsec": "Low Security", "wormhole": "Wormhole Space", "highsec": "High Security"}
        space_label = " + ".join(_snames[s] for s in self.selected_space) if self.selected_space else "*(none)*"
        isk_label   = f"{self.selected_min_isk // 1_000_000:,}M ISK" if self.selected_min_isk else "*(none)*"
        return (
            f"📅 **Daily Report Configuration**\n"
            f"✅ Categories: **{cat_label}**\n"
            f"⏱ Time range: **{time_label}**\n"
            f"🌌 Space: **{space_label}**\n"
            f"💰 Min ISK: **{isk_label}**\n\n"
            "Hit **Save** to apply, or **Cancel** to discard."
        )

    @discord.ui.select(
        placeholder="🚢  Ship categories…",
        min_values=1, max_values=len(SHIP_CATEGORIES),
        options=[
            discord.SelectOption(label=cat["label"], value=key, emoji=cat["emoji"])
            for key, cat in SHIP_CATEGORIES.items()
        ],
    )
    async def category_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        self.selected_categories = select.values
        for opt in select.options:
            opt.default = opt.value in self.selected_categories
        await interaction.response.edit_message(content=self._form_content(), view=self)

    @discord.ui.select(
        placeholder="⏱  Time range…",
        min_values=1, max_values=1,
        options=[
            discord.SelectOption(label=info["label"], value=key)
            for key, info in TIME_RANGES.items()
        ],
    )
    async def time_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        self.selected_time_key = select.values[0]
        for opt in select.options:
            opt.default = opt.value == self.selected_time_key
        await interaction.response.edit_message(content=self._form_content(), view=self)

    @discord.ui.select(
        placeholder="🌌  Space type…",
        min_values=1, max_values=4,
        options=[
            discord.SelectOption(label="Null Security",  value="nullsec",  emoji="⚫"),
            discord.SelectOption(label="Low Security",   value="lowsec",   emoji="🔴"),
            discord.SelectOption(label="Wormhole Space", value="wormhole", emoji="🌀"),
            discord.SelectOption(label="High Security",  value="highsec",  emoji="🟡"),
        ],
    )
    async def space_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        self.selected_space = select.values
        for opt in select.options:
            opt.default = opt.value in self.selected_space
        await interaction.response.edit_message(content=self._form_content(), view=self)

    @discord.ui.select(
        placeholder="💰  Min ISK value… (optional)",
        min_values=0, max_values=1,
        options=[
            discord.SelectOption(label="10M ISK minimum",  value="10000000"),
            discord.SelectOption(label="50M ISK minimum",  value="50000000"),
            discord.SelectOption(label="100M ISK minimum", value="100000000"),
            discord.SelectOption(label="250M ISK minimum", value="250000000"),
            discord.SelectOption(label="500M ISK minimum", value="500000000"),
            discord.SelectOption(label="1B ISK minimum",   value="1000000000"),
        ],
    )
    async def min_isk_select(self, select: discord.ui.Select, interaction: discord.Interaction):
        self.selected_min_isk = int(select.values[0]) if select.values else None
        for opt in select.options:
            opt.default = bool(select.values) and opt.value == select.values[0]
        await interaction.response.edit_message(content=self._form_content(), view=self)

    @discord.ui.button(label="Save", style=discord.ButtonStyle.success, emoji="💾")
    async def save_button(self, _button: discord.ui.Button, interaction: discord.Interaction):
        if not self.selected_categories:
            await interaction.response.edit_message(
                content="⚠️ Select at least one ship category.\n\n" + self._form_content(), view=self
            )
            return
        if not self.selected_space:
            await interaction.response.edit_message(
                content="⚠️ Select at least one space type.\n\n" + self._form_content(), view=self
            )
            return
        _daily_cfg["categories"]  = self.selected_categories
        _daily_cfg["time_key"]    = self.selected_time_key
        _daily_cfg["space_types"] = self.selected_space
        _daily_cfg["min_isk"]     = self.selected_min_isk
        _save_daily_config()
        await interaction.response.edit_message(content="✅ Daily report configuration saved.", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_button(self, _button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

    async def on_timeout(self):
        self.stop()


# ── Daily fetch helper ─────────────────────────────────────────────────────────

async def _run_daily_fetch(channel: discord.TextChannel) -> None:
    """Execute the daily kill fetch and post all results to *channel*."""
    _daily_start = _time.monotonic()
    try:
        kills = await asyncio.wait_for(
            fetch_all_kills(
                category_keys=_daily_cfg["categories"],
                past_seconds=TIME_RANGES[_daily_cfg["time_key"]]["seconds"],
                space_types=_daily_cfg["space_types"],
                min_isk=_daily_cfg.get("min_isk"),
            ),
            timeout=3600,
        )
        if not kills:
            await channel.send("📅 **Daily Kill Report** — no kills found for the configured filters.")
            return
        embed = build_summary_embed(kills, _daily_cfg["categories"], _daily_cfg["time_key"],
                                    elapsed=_time.monotonic() - _daily_start)
        embed.title = "📅 Daily Kill Report  ✅"
        await channel.send(embed=embed)
        try:
            await post_kill_details(channel, kills)
        except Exception as e:
            await channel.send(f"⚠️ Kill details could not be posted: `{type(e).__name__}: {e}`")
    except asyncio.TimeoutError:
        await channel.send("⏱ **Daily scan timed out** after 1 hour — no results posted.")
    except Exception as e:
        await channel.send(f"❌ Daily fetch failed: `{type(e).__name__}: {e}`")


# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    if _daily_cfg.get("enabled") and _daily_cfg.get("channel_id"):
        daily_kill_summary.start()
        channel = bot.get_channel(_daily_cfg["channel_id"])
        if channel:
            embed = discord.Embed(
                description="🟢 **zKill Bot is online and ready.**\nType `/help` to see available commands.",
                color=discord.Color.green(),
            )
            await channel.send(embed=embed)

# ── Slash Commands ────────────────────────────────────────────────────────────

@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Open the kill report filter form",
)
async def scan(ctx: discord.ApplicationContext):
    view = KillFilterView()
    await ctx.respond(content=view._form_content(), view=view, ephemeral=True)


@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Re-post the summary embed from a recent scan (1 = latest, 2 = second-to-last, …)",
)
@discord.option("n", int, description="Which scan to show (1 = latest)", default=1, min_value=1, max_value=5)
async def last(ctx: discord.ApplicationContext, n: int = 1):
    if not _last_embeds:
        await ctx.respond("📋 No scan results yet — run `/scan` first.", ephemeral=True)
        return
    idx = min(n, len(_last_embeds)) - 1
    ordinal = {1: "Latest", 2: "2nd last", 3: "3rd last"}.get(idx + 1, f"#{idx + 1}")
    await ctx.respond(
        content=f"📋 **{ordinal} scan results** (reposted by {ctx.author.mention}):",
        embed=_last_embeds[idx],
    )


@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Show the status of the current scan",
)
async def status(ctx: discord.ApplicationContext):
    if not fetch_in_progress:
        await ctx.respond("💤 No scan is currently running.", ephemeral=True)
        return
    _PHASE_LABELS = {
        "zkill": "Scanning zKillboard",
        "esi":   "Fetching ESI killmails",
        "names": "Resolving names",
        "":      "Starting up",
    }
    phase = _PHASE_LABELS.get(_fetch_phase, _fetch_phase)
    elapsed = ""
    if _fetch_start_ts is not None:
        s = int(_time.monotonic() - _fetch_start_ts)
        m, sec = divmod(s, 60)
        elapsed = f"{m}m {sec:02d}s" if m else f"{sec}s"
    stuck = _fetch_start_ts is not None and (_time.monotonic() - _fetch_start_ts) > 3600
    color = discord.Color.red() if stuck else discord.Color.orange()
    embed = discord.Embed(title="🔍 Scan In Progress", color=color)
    embed.add_field(name="Phase",   value=phase,   inline=True)
    embed.add_field(name="Elapsed", value=elapsed, inline=True)
    if stuck:
        embed.add_field(
            name="⚠️ Warning",
            value="This scan has been running for over 1 hour and may be stuck. Use `/stop` to abort.",
            inline=False,
        )
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Stop the current scan immediately — no results will be posted",
)
async def stop(ctx: discord.ApplicationContext):
    if not fetch_in_progress or _stop_event is None:
        await ctx.respond("⚠️ No scan is currently in progress.", ephemeral=True)
        return
    _stop_event.set()
    await ctx.respond("⏹ **Stopping the current scan.** No results will be posted.")


@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Skip remaining zKillboard scanning and post results collected so far",
)
async def skip(ctx: discord.ApplicationContext):
    if not fetch_in_progress or _skip_event is None:
        await ctx.respond("⚠️ No scan is currently in progress.", ephemeral=True)
        return
    if _fetch_phase != "zkill":
        phase_label = {"esi": "ESI enrichment", "names": "name resolution", "": "starting up"}.get(_fetch_phase, _fetch_phase)
        await ctx.respond(
            f"⚠️ `/skip` is only available during the zKillboard scanning phase (currently: **{phase_label}**).",
            ephemeral=True,
        )
        return
    _skip_event.set()
    await ctx.respond("⏭ **Skipping remaining scan** — will process and post results collected so far.")


@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Show available commands",
)
@discord.option("public", bool, description="Post this message publicly in the channel", default=False)
async def help(ctx: discord.ApplicationContext, public: bool = False):
    embed = discord.Embed(
        title="📖 zKill Bot — Commands",
        description="Scans zKillboard for ship losses in NullSec, LowSec, and Wormhole space.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="🔍 /scan",   value="Open the kill report filter form.", inline=False)
    embed.add_field(name="📋 /last",   value="Re-post the summary embed from the most recent completed scan.", inline=False)
    embed.add_field(name="📊 /status", value="Show the current scan phase and elapsed time.", inline=False)
    embed.add_field(name="⏹ /stop",   value="Abort the current scan immediately. No results are posted.", inline=False)
    embed.add_field(name="⏭ /skip",  value="Skip remaining zKillboard scanning and post partial results. Only usable during the scanning phase.", inline=False)
    embed.add_field(name="🏓 /ping",  value="Check latency.", inline=False)
    embed.add_field(name="📖 /help",  value="Show this message.", inline=False)
    embed.add_field(
        name="📅 /daily  *(admin)*",
        value=(
            "`status` · `configure` · `channel` · `toggle` · `run`\n"
            "Manage the automatic daily kill report."
        ),
        inline=False,
    )
    embed.add_field(
        name="🚫 /exclusions  *(admin)*",
        value=(
            "`add <name>` · `remove <name>` · `list`\n"
            "Manage excluded corporations and alliances (victims only)."
        ),
        inline=False,
    )
    embed.set_footer(text="Data via zKillboard + ESI")
    await ctx.respond(embed=embed, ephemeral=not public)


@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Check the bot is alive",
)
async def ping(ctx: discord.ApplicationContext):
    await ctx.respond(f"🏓 Pong! Latency: `{round(bot.latency * 1000)}ms`")

# ── /daily command group (admin-only) ────────────────────────────────────────

daily_group = bot.create_group(
    "daily",
    "Manage the automatic daily kill report",
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    default_member_permissions=discord.Permissions(administrator=True),
)


@daily_group.command(description="Show the current daily report configuration and next scheduled run")
async def status(ctx: discord.ApplicationContext):
    cfg = _daily_cfg
    _snames = {"nullsec": "⚫ Null Sec", "lowsec": "🔴 Low Sec", "wormhole": "🌀 Wormhole", "highsec": "🟡 High Sec"}
    cat_label   = ", ".join(SHIP_CATEGORIES[k]["label"] for k in cfg["categories"]) or "*(none)*"
    time_label  = TIME_RANGES[cfg["time_key"]]["label"] if cfg["time_key"] in TIME_RANGES else cfg["time_key"]
    space_label = " · ".join(_snames[s] for s in cfg["space_types"] if s in _snames) or "*(none)*"
    isk_label   = f"{cfg['min_isk'] // 1_000_000:,}M ISK" if cfg.get("min_isk") else "*(none)*"
    channel_str = f"<#{cfg['channel_id']}>" if cfg.get("channel_id") else "*(not set)*"
    state       = "🟢 Enabled" if cfg.get("enabled") else "🔴 Disabled"
    next_run    = ""
    if daily_kill_summary.is_running() and daily_kill_summary.next_iteration:
        ts = int(daily_kill_summary.next_iteration.timestamp())
        next_run = f"<t:{ts}:R>"
    embed = discord.Embed(title="📅 Daily Report — Configuration", color=discord.Color.blurple())
    embed.add_field(name="Status",     value=state,      inline=True)
    embed.add_field(name="Channel",    value=channel_str, inline=True)
    embed.add_field(name="Next run",   value=next_run or "*(task not running)*", inline=True)
    embed.add_field(name="Categories", value=cat_label,   inline=False)
    embed.add_field(name="Time range", value=time_label,  inline=True)
    embed.add_field(name="Security",   value=space_label, inline=True)
    embed.add_field(name="Min ISK",    value=isk_label,   inline=True)
    await ctx.respond(embed=embed, ephemeral=True)


@daily_group.command(description="Configure the daily report filters (categories, time range, space, min ISK)")
async def configure(ctx: discord.ApplicationContext):
    view = DailyConfigView()
    await ctx.respond(content=view._form_content(), view=view, ephemeral=True)


@daily_group.command(description="Set the channel where the daily report is posted")
@discord.option("target", discord.TextChannel, description="Channel to post the daily report in")
async def channel(ctx: discord.ApplicationContext, target: discord.TextChannel):
    _daily_cfg["channel_id"] = target.id
    _save_daily_config()
    await ctx.respond(f"✅ Daily report channel set to {target.mention}.", ephemeral=True)


@daily_group.command(description="Enable or disable the automatic daily report")
async def toggle(ctx: discord.ApplicationContext):
    _daily_cfg["enabled"] = not _daily_cfg.get("enabled", False)
    _save_daily_config()
    if _daily_cfg["enabled"]:
        if not daily_kill_summary.is_running():
            global _skip_first_daily
            _skip_first_daily = True   # don't fire immediately on re-enable; wait 24h
            daily_kill_summary.start()
        await ctx.respond("🟢 Daily report **enabled**.", ephemeral=True)
    else:
        if daily_kill_summary.is_running():
            daily_kill_summary.stop()
        await ctx.respond("🔴 Daily report **disabled**.", ephemeral=True)


@daily_group.command(description="Run the daily report immediately with the current configuration")
async def run(ctx: discord.ApplicationContext):
    if fetch_in_progress:
        await ctx.respond("⚠️ A scan is already in progress — wait for it to finish.", ephemeral=True)
        return
    if not _daily_cfg.get("channel_id"):
        await ctx.respond("⚠️ No channel configured. Use `/daily channel` first.", ephemeral=True)
        return
    target = bot.get_channel(_daily_cfg["channel_id"])
    if not target:
        await ctx.respond(f"⚠️ Configured channel `{_daily_cfg['channel_id']}` not found.", ephemeral=True)
        return
    await ctx.respond(f"▶️ Running daily report now — posting to {target.mention}.", ephemeral=True)
    await _run_daily_fetch(target)


# ── Automatic daily summary (optional) ───────────────────────────────────────

@tasks.loop(hours=24)
async def daily_kill_summary():
    global _skip_first_daily
    if _skip_first_daily:
        _skip_first_daily = False
        return
    if not _daily_cfg.get("enabled"):
        return
    channel = bot.get_channel(_daily_cfg["channel_id"])
    if not channel:
        print(f"[WARNING] Daily post channel {_daily_cfg['channel_id']} not found.")
        return
    await _run_daily_fetch(channel)

@daily_kill_summary.before_loop
async def before_daily():
    await bot.wait_until_ready()

@daily_kill_summary.error
async def daily_error(error: Exception):
    print(f"[ERROR] daily_kill_summary crashed: {type(error).__name__}: {error}")
    # Task auto-restarts on the next iteration; log so the issue is visible

# ── /exclusions command group (admin-only) ────────────────────────────────────

class ExclusionConfirmView(discord.ui.View):
    """Confirm / cancel adding a single entity to the exclusion list."""

    def __init__(self, entity: dict):
        super().__init__(timeout=60)
        self.entity = entity

    @discord.ui.button(label="Add", style=discord.ButtonStyle.success)
    async def add_button(self, _button: discord.ui.Button, interaction: discord.Interaction):
        eid      = self.entity["id"]
        ename    = self.entity["name"]
        category = self.entity["category"]   # "corporation" or "alliance"

        if category == "alliance":
            _exclusions_cfg.setdefault("alliances", {})[str(eid)] = ename
            EXCLUDED_ALLIANCE_IDS.add(eid)
        else:
            _exclusions_cfg.setdefault("corporations", {})[str(eid)] = ename
            EXCLUDED_CORP_IDS.add(eid)
        _save_exclusions()

        self.disable_all_items()
        await interaction.response.edit_message(
            content=f"✅ **{ename}** ({category}) added to exclusions.",
            embed=None,
            view=self,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, _button: discord.ui.Button, interaction: discord.Interaction):
        self.disable_all_items()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=self)


exclusions_grp = bot.create_group(
    "exclusions",
    "Manage excluded corporations and alliances",
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    default_member_permissions=discord.Permissions(administrator=True),
)


@exclusions_grp.command(description="Add a corp or alliance to the exclusion list by exact name")
async def add(ctx: discord.ApplicationContext, name: str):
    await ctx.defer(ephemeral=True)
    results = await search_entity_ids(name)

    if not results:
        await ctx.respond("❌ No corporation or alliance found with that exact name.", ephemeral=True)
        return

    if len(results) > 1:
        lines = [
            f"• **{r['name']}** (ID `{r['id']}`) — {r['category']}"
            for r in results
        ]
        await ctx.respond(
            f"Found **{len(results)} matches** — please be more specific:\n" + "\n".join(lines),
            ephemeral=True,
        )
        return

    entity = results[0]
    existing_ids = {
        int(k) for k in list(_exclusions_cfg.get("alliances", {})) + list(_exclusions_cfg.get("corporations", {}))
    } | EXCLUDED_ALLIANCE_IDS | EXCLUDED_CORP_IDS
    if entity["id"] in existing_ids:
        await ctx.respond(
            f"⚠️ **{entity['name']}** is already in the exclusion list.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="Add exclusion?",
        description=(
            f"**{entity['name']}**\n"
            f"Category: {entity['category']}\n"
            f"ID: `{entity['id']}`"
        ),
        color=discord.Color.orange(),
    )
    view = ExclusionConfirmView(entity)
    await ctx.respond(embed=embed, view=view, ephemeral=True)


@exclusions_grp.command(description="Remove a corp or alliance from the exclusion list by name")
async def remove(ctx: discord.ApplicationContext, name: str):
    name_lower = name.strip().lower()
    removed: list[str] = []

    for eid, ename in list(_exclusions_cfg.get("alliances", {}).items()):
        if ename.lower() == name_lower:
            del _exclusions_cfg["alliances"][eid]
            EXCLUDED_ALLIANCE_IDS.discard(int(eid))
            removed.append(f"**{ename}** (alliance)")

    for eid, ename in list(_exclusions_cfg.get("corporations", {}).items()):
        if ename.lower() == name_lower:
            del _exclusions_cfg["corporations"][eid]
            EXCLUDED_CORP_IDS.discard(int(eid))
            removed.append(f"**{ename}** (corporation)")

    if not removed:
        await ctx.respond(
            f"❌ No exclusion found matching `{name}`.\n"
            "Note: entries added via `.env` cannot be removed here — edit the env file instead.",
            ephemeral=True,
        )
        return

    _save_exclusions()
    await ctx.respond(f"✅ Removed: {', '.join(removed)}", ephemeral=True)


@exclusions_grp.command(name="list", description="Show all excluded corporations and alliances")
async def exclusions_list(ctx: discord.ApplicationContext):
    lines: list[str] = []

    for eid, ename in sorted(_exclusions_cfg.get("alliances", {}).items(), key=lambda x: x[1].lower()):
        lines.append(f"🔷 **{ename}** — alliance · ID `{eid}`")
    for eid, ename in sorted(_exclusions_cfg.get("corporations", {}).items(), key=lambda x: x[1].lower()):
        lines.append(f"🔶 **{ename}** — corp · ID `{eid}`")

    # Show IDs that came from .env and have no name in exclusions.json
    json_alliance_ids = {int(k) for k in _exclusions_cfg.get("alliances", {})}
    json_corp_ids     = {int(k) for k in _exclusions_cfg.get("corporations", {})}
    for eid in sorted(EXCLUDED_ALLIANCE_IDS - json_alliance_ids):
        lines.append(f"🔷 ID `{eid}` — alliance · *from .env*")
    for eid in sorted(EXCLUDED_CORP_IDS - json_corp_ids):
        lines.append(f"🔶 ID `{eid}` — corp · *from .env*")

    if not lines:
        await ctx.respond("No exclusions configured.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Excluded corps & alliances",
        description="\n".join(lines),
        color=discord.Color.red(),
    )
    await ctx.respond(embed=embed, ephemeral=True)


# ── Graceful shutdown notice ──────────────────────────────────────────────────

_original_close = bot.close

async def _close_with_notice():
    if _daily_cfg.get("channel_id"):
        channel = bot.get_channel(_daily_cfg["channel_id"])
        if channel:
            embed = discord.Embed(
                description="🔴 **zKill Bot is going offline.**",
                color=discord.Color.dark_red(),
            )
            try:
                await channel.send(embed=embed)
            except Exception:
                pass
    await _original_close()

bot.close = _close_with_notice

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env file.")
    bot.run(TOKEN)
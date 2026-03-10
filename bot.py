"""
bot.py — py-cord Discord bot with interactive filter form.
"""

import asyncio
import collections
import io
import os
import time as _time
from datetime import datetime, timezone
import discord
from discord.ext import tasks
from dotenv import load_dotenv

from fetcher import fetch_all_kills, SHIP_CATEGORIES, TIME_RANGES

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

DEV_GUILD_ID         = 671707585262649344   # e.g. 123456789012345678 — set for instant dev registration
AUTO_POST_CHANNEL_ID = 1480291942389514370   # e.g. 987654321098765432 — set to enable daily auto-post
AUTO_POST_CATEGORIES = ["industrial", "mining"]   # categories used by the daily auto-post
AUTO_POST_TIME_KEY   = "24h"                       # time range key used by the daily auto-post
MAX_SCAN_RESULTS: int | None = None                # cap zKillboard scanning early (e.g. 100); None = no cap
HIGHSEC_MAX_RESULTS: int = 200                     # automatic cap when High Security space is selected

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

def build_summary_embed(kills: list[dict], category_keys: list[str], time_key: str) -> discord.Embed:
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
            f"**{cat_names}**\n"
            f"\n"
            f"⏱  {time_label}\n"
            f"🌌  {space_str}"
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
            embed.add_field(name="\u200b", value="\u200b", inline=False)
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
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        embed.add_field(name="🌌 By security", value="\n".join(space_lines), inline=False)

    embed.set_footer(text="Data via zKillboard + ESI")
    return embed

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

        def build_status() -> str:
            sep = "─" * 48
            eta = f"  (est. {_fmt_est(est_seconds[0])})" if est_seconds[0] else ""
            log_lines = "\n".join(log_buffer)
            isk_line    = f"Min ISK:     {isk_label}\n" if isk_label else ""
            region_line = f"Regions:     {region_label[:42]}\n" if region_label else ""
            return (
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
            kills = await fetch_all_kills(
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
                embed = build_summary_embed(kills, self.selected_categories, time_key)
                _last_embeds.appendleft(embed)
                await channel.send(content=f"{caller.mention} — kill report ready!", embed=embed)

                # ── Detailed kill list (pilot | ship | ISK | system | time | link) ──
                _SEC_EMOJI = {"nullsec": "⚫", "lowsec": "🔴", "wormhole": "🌀", "highsec": "🟡"}

                def _discord_ts(km_time: str) -> str:
                    """Return a Discord relative timestamp tag, e.g. <t:1234567890:R>."""
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

                detail_lines = []
                for k in kills:
                    pilot = k.get("pilot_name", "")
                    if not pilot or pilot in ("Unknown", "Unknown (NPC)"):
                        continue
                    sec_e  = _SEC_EMOJI.get(k.get("space_type", ""), "")
                    ship   = k.get("ship_name") or f"Ship {k.get('ship_type_id', '?')}"
                    isk    = k.get("total_value", 0) / 1_000_000
                    url    = k.get("zkill_url", "")
                    corp   = k.get("corp_name", "")
                    system = k.get("solar_system_name", "?")
                    ts     = _utc_ts(k.get("killmail_time", ""))
                    detail_lines.append(f"{sec_e} {pilot} | {corp} | {ship} | {isk:.0f}M ISK | {system} | {ts} | {url}")

                if detail_lines:
                    # Use <url> format: clickable in Discord messages and suppresses link preview boxes
                    md_lines = []
                    for k in kills:
                        pilot = k.get("pilot_name", "")
                        if not pilot or pilot in ("Unknown", "Unknown (NPC)"):
                            continue
                        sec_e  = _SEC_EMOJI.get(k.get("space_type", ""), "")
                        ship   = k.get("ship_name") or f"Ship {k.get('ship_type_id', '?')}"
                        isk    = k.get("total_value", 0) / 1_000_000
                        url    = k.get("zkill_url", "")
                        corp   = k.get("corp_name", "")
                        system = k.get("solar_system_name", "?")
                        ts     = _discord_ts(k.get("killmail_time", ""))
                        corp_part = f" ({corp})" if corp else ""
                        md_lines.append(f"{sec_e} **{pilot}**{corp_part} — {ship} — {isk:.0f}M ISK — 📍 {system} — {ts} — 🔗 <{url}>")

                    d_header = f"📋 **Kill details** ({len(md_lines)} kills):\n"
                    md_block = "\n".join(md_lines)
                    if len(d_header) + len(md_block) <= 1900:
                        await channel.send(content=f"{d_header}{md_block}")
                    else:
                        # Too long for one message — send as plain-text file (URLs still copyable)
                        plain_block = "\n".join(detail_lines)
                        buf = io.BytesIO(plain_block.encode("utf-8"))
                        await channel.send(content=d_header, file=discord.File(buf, filename="kills.txt"))

                # ── Comma-separated pilot names for in-game mail ──────────────
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
                    comma_list = ", ".join(unique_names)
                    m_header = f"📮 **In-game mail list** ({len(unique_names)} pilots):\n"
                    if len(comma_list) <= 1800:
                        await channel.send(content=f"{m_header}```\n{comma_list}\n```")
                    else:
                        buf = io.BytesIO(comma_list.encode("utf-8"))
                        await channel.send(content=m_header, file=discord.File(buf, filename="pilot_names.txt"))

                    # ── EVE in-game mail body format (showinfo links) ─────────
                    eve_links = "".join(
                        f'<a href="showinfo:1375//{p["cid"]}">{p["name"]}</a><br>'
                        for p in unique_pilots
                    )
                    eve_body = f'<font size="14" color="#ffd98d00">{eve_links}</font>'
                    e_header = f"📨 **EVE mail body** ({len(unique_pilots)} pilots):\n"
                    if len(eve_body) <= 1800:
                        await channel.send(content=f"{e_header}```\n{eve_body}\n```")
                    else:
                        buf = io.BytesIO(eve_body.encode("utf-8"))
                        await channel.send(content=e_header, file=discord.File(buf, filename="eve_mail.txt"))

                await status_msg.edit(content="✅ Done!")
        except Exception as e:
            await status_msg.edit(content=f"❌ Fetch failed: `{e}`")
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

# ── Events ────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")
    if AUTO_POST_CHANNEL_ID:
        daily_kill_summary.start()
        channel = bot.get_channel(AUTO_POST_CHANNEL_ID)
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
async def last(
    ctx: discord.ApplicationContext,
    n: discord.Option(int, "Which scan to show (1 = latest)", default=1, min_value=1, max_value=5) = 1,
):
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
    embed = discord.Embed(title="🔍 Scan In Progress", color=discord.Color.orange())
    embed.add_field(name="Phase",   value=phase,   inline=True)
    embed.add_field(name="Elapsed", value=elapsed, inline=True)
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
async def help(ctx: discord.ApplicationContext):
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
    embed.set_footer(text="Data via zKillboard + ESI")
    await ctx.respond(embed=embed, ephemeral=True)


@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Check the bot is alive",
)
async def ping(ctx: discord.ApplicationContext):
    await ctx.respond(f"🏓 Pong! Latency: `{round(bot.latency * 1000)}ms`")

# ── Automatic daily summary (optional) ───────────────────────────────────────

@tasks.loop(hours=24)
async def daily_kill_summary():
    global _skip_first_daily
    if _skip_first_daily:
        _skip_first_daily = False
        return
    channel = bot.get_channel(AUTO_POST_CHANNEL_ID)
    if not channel:
        print(f"[WARNING] Auto-post channel {AUTO_POST_CHANNEL_ID} not found.")
        return
    try:
        past_seconds = TIME_RANGES[AUTO_POST_TIME_KEY]["seconds"]
        kills        = await fetch_all_kills(
            category_keys=AUTO_POST_CATEGORIES,
            past_seconds=past_seconds,
        )
        embed = build_summary_embed(kills, AUTO_POST_CATEGORIES, AUTO_POST_TIME_KEY)
        await channel.send(embed=embed)
    except Exception as e:
        await channel.send(f"❌ Scheduled fetch failed: `{e}`")

@daily_kill_summary.before_loop
async def before_daily():
    await bot.wait_until_ready()

# ── Graceful shutdown notice ──────────────────────────────────────────────────

_original_close = bot.close

async def _close_with_notice():
    if AUTO_POST_CHANNEL_ID:
        channel = bot.get_channel(AUTO_POST_CHANNEL_ID)
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
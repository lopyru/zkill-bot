"""
bot.py — py-cord Discord bot with interactive filter form.
"""

import asyncio
import collections
import io
import os
import time as _time
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

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
bot     = discord.Bot(intents=intents)

fetch_in_progress = False
_skip_first_daily = True   # skip the immediate fire on startup; run after first 24h interval

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

    _SPACE  = {"nullsec": "⚫ Null Sec", "lowsec": "🔴 Low Sec", "wormhole": "🌀 Wormhole"}
    present = {k.get("space_type") for k in kills if k.get("space_type")}

    embed = discord.Embed(
        title="Pilots deaths report ready! ✅",
        description=(
            f"**📁 {cat_names}**\n"
            f"⏱ {time_label}\n"
            f"🌌 **Security**\n"
            + " ".join(v for s, v in _SPACE.items() if s in present)
        ),
        color=discord.Color.red(),
    )
    embed.add_field(name="💀 Kills",         value=f"{len(kills):,}",   inline=True)
    embed.add_field(name="👤 Unique pilots", value=f"{unique_pilots:,}", inline=True)
    embed.add_field(name="💰 Total ISK",     value=value_str,            inline=True)

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
                    f"**{len(cat_kills)}** kills · {isk_s} ISK"
                )
        if cat_lines:
            embed.add_field(name="📊 By category", value="\n".join(cat_lines), inline=False)

    # Breakdown by security
    space_lines = []
    for s_key in ("nullsec", "lowsec", "wormhole"):
        s_kills = [k for k in kills if k.get("space_type") == s_key]
        if s_kills:
            emoji = {"nullsec": "⚫", "lowsec": "🔴", "wormhole": "🌀"}[s_key]
            label = {"nullsec": "Null Sec", "lowsec": "Low Sec", "wormhole": "Wormhole"}[s_key]
            isk   = sum(k.get("total_value", 0) for k in s_kills)
            isk_s = f"{isk/1e9:.2f}B" if isk >= 1e9 else f"{isk/1e6:.0f}M"
            space_lines.append(f"{emoji} {label}: **{len(s_kills)}** kills · {isk_s} ISK")
    if space_lines:
        embed.add_field(name="🌌 By security", value="\n".join(space_lines), inline=False)

    embed.set_footer(text="Data via zKillboard + ESI")
    return embed

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

    # ── Ship category multi-select ────────────────────────────────────────────

    @discord.ui.select(
        placeholder="🚢  Choose ship categories…",
        min_values=0,
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

        label = ", ".join(
            SHIP_CATEGORIES[k]["label"] for k in self.selected_categories
        ) or "All ships (no filter)"
        space_label = " + ".join({"nullsec": "Null Security", "lowsec": "Low Security", "wormhole": "Wormhole Space"}[s] for s in self.selected_space)
        await interaction.response.edit_message(
            content=f"✅ Categories: **{label}**\n⏱ Time range: **{TIME_RANGES[self.selected_time_key]['label']}**\n🌌 Space: **{space_label}**\n\nHit **Fetch** when ready.",
            view=self,
        )

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

        label = ", ".join(
            SHIP_CATEGORIES[k]["label"] for k in self.selected_categories
        ) or "All ships (no filter)"
        space_label = " + ".join({"nullsec": "Null Security", "lowsec": "Low Security", "wormhole": "Wormhole Space"}[s] for s in self.selected_space)
        await interaction.response.edit_message(
            content=f"✅ Categories: **{label}**\n⏱ Time range: **{TIME_RANGES[self.selected_time_key]['label']}**\n🌌 Space: **{space_label}**\n\nHit **Fetch** when ready.",
            view=self,
        )

    # ── Space type multi-select ───────────────────────────────────────────────

    @discord.ui.select(
        placeholder="🌌  Space type…",
        min_values=1,
        max_values=3,
        options=[
            discord.SelectOption(label="Null Security",  value="nullsec",  emoji="⚫", default=True),
            discord.SelectOption(label="Low Security",   value="lowsec",   emoji="🔴", default=True),
            discord.SelectOption(label="Wormhole Space", value="wormhole", emoji="🌀", default=False),
        ],
    )
    async def space_select(
        self, select: discord.ui.Select, interaction: discord.Interaction
    ):
        self.selected_space = select.values
        for option in select.options:
            option.default = option.value in self.selected_space

        cat_label   = ", ".join(SHIP_CATEGORIES[k]["label"] for k in self.selected_categories) or "All ships (no filter)"
        space_label = " + ".join(o.label for o in select.options if o.default)
        await interaction.response.edit_message(
            content=f"✅ Categories: **{cat_label}**\n⏱ Time range: **{TIME_RANGES[self.selected_time_key]['label']}**\n🌌 Space: **{space_label}**\n\nHit **Fetch** when ready.",
            view=self,
        )

    # ── Fetch button ──────────────────────────────────────────────────────────

    @discord.ui.button(label="Fetch", style=discord.ButtonStyle.danger, emoji="🔍")
    async def fetch_button(
        self, _button: discord.ui.Button, interaction: discord.Interaction
    ):
        global fetch_in_progress

        # Fix #2: keep the form visible and send a separate ephemeral notice
        if fetch_in_progress:
            await interaction.response.send_message(
                "⚠️ A fetch is already in progress — please wait for it to finish.",
                ephemeral=True,
            )
            return

        fetch_in_progress = True
        caller     = interaction.user
        channel    = interaction.channel
        time_key   = self.selected_time_key
        time_label = TIME_RANGES[time_key]["label"]
        cat_label  = (
            ", ".join(SHIP_CATEGORIES[k]["label"] for k in self.selected_categories)
            or "All ships"
        )
        past_seconds = TIME_RANGES[time_key]["seconds"]
        categories   = self.selected_categories or None

        # ── Live progress state ───────────────────────────────────────────────
        W = 14  # progress bar width (chars)
        _SPACE_LABELS = {"nullsec": "⚫ Null Sec", "lowsec": "🔴 Low Sec", "wormhole": "🌀 Wormhole"}
        space_label = " · ".join(_SPACE_LABELS[s] for s in self.selected_space if s in _SPACE_LABELS)
        stages = {
            "regions": "[ ] Classifying NS / LS / WH regions...",
            "zkill":   "[ ] Waiting for region list...",
            "esi":     "[ ] Pending kill data",
            "names":   "[ ] Pending ESI enrichment",
        }
        log_buffer  = collections.deque(maxlen=6)
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
            log_lines = "\n".join(log_buffer) if log_buffer else "  (waiting for activity...)"
            return (
                f"```\n"
                f"Categories:  {cat_label[:36]}\n"
                f"Time range:  {time_label}\n"
                f"Security:    {space_label}\n"
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

        async def _edit_loop():
            while not _loop_stop[0]:
                await asyncio.sleep(0.5)
                try:
                    await status_msg.edit(content=build_status())
                except Exception:
                    pass

        loop_task = asyncio.ensure_future(_edit_loop())

        async def on_progress(event: dict):
            phase = event.get("phase")
            if phase == "regions":
                stages["regions"] = f"[✓] {event['count']} regions classified"
                stages["zkill"]   = "[~] Scanning ship kills across all selected regions..."
            elif phase == "zkill_start":
                total = event["types"]
                est_seconds[0] = total   # 1 req/s → total seconds
                stages["zkill"] = f"[~] {_bar(0, total)}  0/{total}  — querying zKillboard"
            elif phase == "zkill_progress":
                done, total, found = event["done"], event["total"], event["found"]
                stages["zkill"] = f"[~] {_bar(done, total)}  {done}/{total}  ({found} hits)"
            elif phase == "zkill_done":
                found = event["found"]
                stages["zkill"] = f"[✓] {found} kill{'s' if found != 1 else ''} matched"
                stages["esi"]   = f"[~] {_bar(0, found)}  0/{found}  — fetching ESI killmails"
            elif phase == "esi":
                done, total = event["done"], event["total"]
                stages["esi"] = f"[~] {_bar(done, total)}  {done}/{total}  — enriching kills"
            elif phase == "names":
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
            )
            if not kills:
                await channel.send(
                    content=(
                        f"{caller.mention} — no kills found for **{cat_label}** "
                        f"in the last **{time_label}** in **{space_label}**."
                    )
                )
                await status_msg.edit(content="✅ Done — no results.")
            else:
                embed = build_summary_embed(kills, self.selected_categories, time_key)
                # Fix #4: tag the caller when results are ready
                await channel.send(content=f"{caller.mention} — kill report ready!", embed=embed)

                # ── Detailed kill list (pilot | ship | ISK | link) ────────────
                detail_lines = []
                for k in kills:
                    pilot = k.get("pilot_name", "")
                    if not pilot or pilot in ("Unknown", "Unknown (NPC)"):
                        continue
                    ship  = k.get("ship_name") or f"Ship {k.get('ship_type_id', '?')}"
                    isk   = k.get("total_value", 0) / 1_000_000
                    url   = k.get("zkill_url", "")
                    detail_lines.append(f"{pilot} | {ship} | {isk:.0f}M ISK | {url}")

                if detail_lines:
                    # Use <url> format: clickable in Discord messages and suppresses link preview boxes
                    md_lines = []
                    for k in kills:
                        pilot = k.get("pilot_name", "")
                        if not pilot or pilot in ("Unknown", "Unknown (NPC)"):
                            continue
                        ship = k.get("ship_name") or f"Ship {k.get('ship_type_id', '?')}"
                        isk  = k.get("total_value", 0) / 1_000_000
                        url  = k.get("zkill_url", "")
                        md_lines.append(f"**{pilot}** — {ship} — {isk:.0f}M ISK — 🔗 <{url}>")

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
            fetch_in_progress = False

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
                description="🟢 **zKill Bot is online and ready.**",
                color=discord.Color.green(),
            )
            await channel.send(embed=embed)

# ── Slash Commands ────────────────────────────────────────────────────────────

@bot.slash_command(
    guild_ids=[DEV_GUILD_ID] if DEV_GUILD_ID else None,
    description="Fetch NullSec + LowSec killmails with custom filters",
)
async def kills(ctx: discord.ApplicationContext):
    view = KillFilterView()
    await ctx.respond(
        content=(
            "**Kill Report Filters**\n"
            "Select ship categories, time range, and space type, then hit **Fetch**.\n"
            "Leave categories empty to fetch all ships.\n\n"
            "✅ Categories: **All ships (no filter)**\n"
            "⏱ Time range: **Last 24 hours**\n"
            "🌌 Space: **Null Security + Low Security**"
        ),
        view=view,
        ephemeral=True,
    )


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
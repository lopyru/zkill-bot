"""
bot.py — py-cord Discord bot with interactive filter form.
"""

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
    total_value    = sum(k.get("total_value", k.get("zkb", {}).get("totalValue", 0)) for k in kills)
    value_str      = f"{total_value / 1_000_000_000:.2f}B ISK" if total_value else "N/A"
    category_names = " · ".join(SHIP_CATEGORIES[k]["label"] for k in category_keys) if category_keys else "All ships"
    time_label     = TIME_RANGES[time_key]["label"] if time_key in TIME_RANGES else time_key

    embed = discord.Embed(
        title=f"💀 Kill Report — {time_label}",
        description=f"**Categories:** {category_names}",
        color=discord.Color.red(),
    )
    embed.add_field(name="Total Kills",          value=f"{len(kills):,}",  inline=True)
    embed.add_field(name="Total ISK Destroyed",  value=value_str,          inline=True)

    # Show up to 10 kills inline
    if kills:
        lines = []
        for k in kills[:10]:
            pilot = k.get("pilot_name", "Unknown")
            value = k.get("total_value", 0)
            url   = k.get("zkill_url", "")
            lines.append(f"[{pilot}]({url}) — {value / 1_000_000:.0f}M ISK")
        if len(kills) > 10:
            lines.append(f"*...and {len(kills) - 10} more*")
        embed.add_field(name="Kills", value="\n".join(lines), inline=False)

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
        stages = {
            "regions": "[ ] discovering...",
            "zkill":   "[ ] —",
            "esi":     "[ ] —",
            "names":   "[ ] —",
        }
        last_edit   = [0.0]
        start_ts    = [_time.monotonic()]
        est_seconds = [0]

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
            sep = "─" * 44
            eta = f"  (est. {_fmt_est(est_seconds[0])})" if est_seconds[0] else ""
            return (
                f"```\n"
                f"▶  {cat_label[:38]}  /  {time_label}\n"
                f"{sep}\n"
                f"Regions     {stages['regions']}\n"
                f"zKillboard  {stages['zkill']}\n"
                f"ESI         {stages['esi']}\n"
                f"Names       {stages['names']}\n"
                f"{sep}\n"
                f"Elapsed     {_elapsed()}{eta}\n"
                f"```"
            )

        # Fix #3: dismiss the ephemeral form, open a public progress panel
        await interaction.response.edit_message(
            content=f"🔍 Fetching… see progress in <#{channel.id}>", view=None
        )
        status_msg = await channel.send(content=build_status())

        async def on_progress(event: dict):
            phase = event.get("phase")
            if phase == "regions":
                stages["regions"] = f"[✓] {event['count']} regions"
                stages["zkill"]   = "[~] starting..."
            elif phase == "zkill_start":
                total = event["types"]
                est_seconds[0] = total   # 1 req/s → total seconds
                stages["zkill"] = f"[~] {_bar(0, total)}  0/{total}"
            elif phase == "zkill_progress":
                done, total, found = event["done"], event["total"], event["found"]
                stages["zkill"] = f"[~] {_bar(done, total)}  {done}/{total}  ({found} hits)"
            elif phase == "zkill_done":
                found = event["found"]
                stages["zkill"] = f"[✓] {found} kill{'s' if found != 1 else ''} found"
                stages["esi"]   = f"[~] {_bar(0, found)}  0/{found}"
            elif phase == "esi":
                done, total = event["done"], event["total"]
                stages["esi"] = f"[~] {_bar(done, total)}  {done}/{total}"
            elif phase == "names":
                stages["esi"]   = stages["esi"].replace("[~]", "[✓]")
                stages["names"] = "[~] resolving..."

            # Throttle Discord edits to ~1 per 2 seconds
            now = _time.monotonic()
            if now - last_edit[0] < 2.0:
                return
            last_edit[0] = now
            try:
                await status_msg.edit(content=build_status())
            except Exception:
                pass

        try:
            kills = await fetch_all_kills(
                category_keys=categories,
                past_seconds=past_seconds,
                space_types=self.selected_space,
                on_progress=on_progress,
            )
            embed = build_summary_embed(kills, self.selected_categories, time_key)
            # Fix #4: tag the caller when results are ready
            await channel.send(content=f"{caller.mention} — kill report ready!", embed=embed)

            # ── Copyable pilot list for in-game mail ──────────────────────────
            if kills:
                names = [k.get("pilot_name") for k in kills
                         if k.get("pilot_name") not in (None, "Unknown", "Unknown (NPC)")]
                if names:
                    seen: set[str] = set()
                    unique_names = [n for n in names if not (n in seen or seen.add(n))]
                    pilot_block = "\n".join(unique_names)
                    header = f"📋 **Pilot list** ({len(unique_names)} pilots) — copy into EVE in-game mail:\n"
                    if len(pilot_block) <= 1800:
                        await channel.send(content=f"{header}```\n{pilot_block}\n```")
                    else:
                        buf = io.BytesIO(pilot_block.encode("utf-8"))
                        await channel.send(
                            content=header,
                            file=discord.File(buf, filename="pilot_list.txt"),
                        )

            await status_msg.edit(content="✅ Done!")
        except Exception as e:
            await status_msg.edit(content=f"❌ Fetch failed: `{e}`")
        finally:
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

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN not set in .env file.")
    bot.run(TOKEN)
"""Novus applicant KC screening and staff gear review, separate from approval and tenure."""
import asyncio
import datetime
from zoneinfo import ZoneInfo
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from urllib.parse import quote

import discord
from discord.ext import commands

from common import WOM_API_KEY, sanitize_rsn

logger = logging.getLogger("WOMBot")
GUILD = 1532817291048124466
CATEGORY = 1560643746188632205
TEST_CHANNEL = 1575015372502081546
STAFF = {1532829195657285843, 1532830356875513906, 1532830960062693396, 1533587986204069908}
RAIDS = (
    ("CoX", "chambers_of_xeric"),
    ("CoX CM", "chambers_of_xeric_challenge_mode"),
    ("ToB", "theatre_of_blood"),
    ("ToB HM", "theatre_of_blood_hard_mode"),
    ("ToA Expert", "tombs_of_amascut_expert"),
    ("TFA", "the_fractured_archive"),
)
EXTRAS = (("Inferno", "tzkal_zuk"), ("Colosseum", "sol_heredit"))
TRIAL_REFERENCE = (
    ("Doom of Mokhaiotl", "doom_of_mokhaiotl"),
    ("Phosani's Nightmare", "phosanis_nightmare"),
)
REPORT_TITLE = "Novus Applicant Eligibility"

APPLICATION_INTRO = """## Thanks for applying to Novus!

Please provide the following if you haven't already:

- **Account type:** Main or Ironman
- **Gear screenshots**
- **How you found us**

If you're just short of our requirements or applying through Raids Prep or Raid Trial, let us know!

-# Your PvM statistics and KC eligibility are automatically retrieved through Wise Old Man."""


def db():
    return sqlite3.connect("wom_multi.db")

def initialize():
    with closing(db()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS application_kc_cache (guild_id INTEGER, discord_id INTEGER, rsn TEXT, payload TEXT, PRIMARY KEY(guild_id, discord_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS application_kc_reports (channel_id INTEGER PRIMARY KEY, applicant_id INTEGER, rsn TEXT NOT NULL, message_id INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS application_gear_reviews (channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, reviewer_id INTEGER NOT NULL, verified_at TEXT NOT NULL, PRIMARY KEY(channel_id, message_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS application_final_approvals (channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, reviewer_id INTEGER NOT NULL, pathway TEXT NOT NULL, approved_at TEXT NOT NULL, PRIMARY KEY(channel_id, message_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS application_message_tests (test_key TEXT PRIMARY KEY, message_id INTEGER NOT NULL)")

def get_report(channel_id):
    with closing(db()) as conn:
        return conn.execute("SELECT applicant_id, rsn, message_id FROM application_kc_reports WHERE channel_id=?", (channel_id,)).fetchone()

def save_report(channel_id, applicant_id, rsn, message_id):
    with closing(db()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO application_kc_reports VALUES (?, ?, ?, ?)", (channel_id, applicant_id, rsn, message_id))

def gear_reviewer(channel_id, message_id):
    with closing(db()) as conn:
        row = conn.execute("SELECT reviewer_id FROM application_gear_reviews WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone()
    return row[0] if row else None

def set_gear_review(channel_id, message_id, reviewer_id):
    with closing(db()) as conn, conn:
        if reviewer_id is None:
            conn.execute("DELETE FROM application_gear_reviews WHERE channel_id=? AND message_id=?", (channel_id, message_id))
        else:
            conn.execute("INSERT OR REPLACE INTO application_gear_reviews VALUES (?, ?, ?, ?)", (channel_id, message_id, reviewer_id, datetime.datetime.now(datetime.timezone.utc).isoformat()))

def gear_status(reviewer_id):
    return f"✅ **Gear requirement met** (verified by <@{reviewer_id}>)" if reviewer_id else "Gear verification pending"

PATHWAYS = {
    "standard": "200+ total raid KC",
    "alternative": "100+ raid KC + Inferno/Colosseum",
    "rpp": "Raids Prep Program completed",
    "raidtrial": "Raid Trial completed (3 member vouches)",
}

def final_approval(channel_id, message_id):
    with closing(db()) as conn:
        return conn.execute("SELECT reviewer_id, pathway FROM application_final_approvals WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone()

def save_final_approval(channel_id, message_id, reviewer_id, pathway):
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO application_final_approvals VALUES (?, ?, ?, ?, ?)", (channel_id, message_id, reviewer_id, pathway, datetime.datetime.now(datetime.timezone.utc).isoformat()))

WELCOME_TEXT = """## Welcome to Novus!

**🌍 Home World** • 346
⠀
**Looking for a Group**
Grab PvM roles and pings in {assign_roles}, or start your own group in {create_lfg}.
⠀
**{raid_elder}Novus Raid Academy**
Learn a raid, sharpen your mechanics, or apply to become a Raid Elder in {raid_academy_info}.
⠀
**{paragon}PvM Diary**
Check the requirements in {info_and_apply} and submit your diary in {make_a_ticket}.
⠀
**{pb1}PB Leaderboard**
See clan records in {pb_leaderboard} and submit your own with \x60/pbsubmit\x60.
⠀
**Raid Resources**
{tob_guide} • {toa_guide} • {tfa_guide}
⠀
### PvM is split by default
Any FFA arrangement, including for Ironmen, must be agreed to by **every team member before the activity starts**.
-# The player requesting FFA is responsible for keeping proof of the agreement.
⠀
Don’t be shy about asking questions or jumping into a group. See you on 346!"""


def welcome_text(guild):
    channel_names = (
        "assign-roles", "create-lfg", "raid-academy-info", "info-and-apply",
        "make-a-ticket", "pb-leaderboard", "tob-guide", "toa-guide", "tfa-guide",
    )
    tokens = {}
    for name in channel_names:
        matches = [c for c in guild.text_channels if c.name == name]
        tokens[name.replace("-", "_")] = matches[0].mention if len(matches) == 1 else "#" + name
        if len(matches) != 1:
            logger.warning("Welcome template: expected one channel named %s; found %s", name, len(matches))
    emoji_aliases = {
        "raid_elder": {"raidelder", "raid_elder"},
        "paragon": {"paragon"},
        "pb1": {"pb1", "novuspb1"},
    }
    for key, aliases in emoji_aliases.items():
        matches = [emoji for emoji in guild.emojis if emoji.name.casefold() in aliases and emoji.available]
        exact = [emoji for emoji in matches if emoji.name.casefold() == key]
        chosen = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
        tokens[key] = f"{chosen} " if chosen else ""
    return WELCOME_TEXT.format(**tokens)


def is_staff(member):
    return any(role.id in STAFF for role in getattr(member, "roles", ()))

def primary_name(nickname):
    value = re.split(r"[|/]", nickname or "")[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9 _-]{1,12}", value) or not value.strip(" _-"):
        return None
    return sanitize_rsn(value)

def extract(data):
    snapshot = data.get("latestSnapshot") or {}
    bosses = (snapshot.get("data") or {}).get("bosses") or {}
    values = {}
    for _, key in RAIDS + EXTRAS + TRIAL_REFERENCE:
        metric = bosses.get(key) or {}
        kc = metric.get("kills")
        values[key] = kc if type(kc) is int and kc >= 0 else None
    return {"values": values, "snapshot": snapshot.get("createdAt"), "name": data.get("displayName") or data.get("username")}

def report_embed(rsn, payload, test=False, note=None, reviewer_id=None):
    payload = payload or {}
    values = payload.get("values", {})
    total = sum(values.get(key) or 0 for _, key in RAIDS)
    incomplete = any(values.get(key) is None for _, key in RAIDS)
    special = any((values.get(key) or 0) >= 1 for _, key in EXTRAS)
    special_unknown = any(values.get(key) is None for _, key in EXTRAS)
    standard = total >= 200
    alternative = total >= 100 and special
    target = 100 if special and not standard else 200
    qualified = standard or alternative
    uncertain = not qualified and (incomplete or (total >= 100 and special_unknown))
    total_indicator = "✅ " if qualified else ("" if uncertain else "❌ ")
    if standard or alternative:
        pathway = "200+ raid KC" if standard else "100+ raid KC + Inferno/Colosseum"
        result = f"✅ **KC requirement met** ({pathway})"
        color = discord.Color.green()
    elif uncertain:
        result = "**KC assessment incomplete**"
        color = discord.Color.gold()
    else:
        result = "❌ **KC thresholds not met**"
        color = discord.Color.red()

    def fmt(key):
        value = values.get(key)
        return f"{value:,}" if value is not None else "N/A"

    name = discord.utils.escape_markdown(payload.get("name") or rsn)
    total_label = f"{'≥ ' if incomplete else ''}{total:,} / {target:,} KC"
    embed = discord.Embed(
        title=REPORT_TITLE,
        description=f"**Applicant:** {name}\n\n**Total Raid KC:** {total_indicator}**{total_label}**",
        color=color,
    )
    # Monospace rows retain their alignment on both mobile and desktop.
    rows = "\n".join(f"{label:<11} {fmt(key):>7}" for label, key in RAIDS)
    embed.add_field(name="Raid KC", value=f"\x60\x60\x60\n{rows}\n\x60\x60\x60", inline=False)
    solo_rows = "\n".join(
        f"**{label}:** {fmt(key)}" + (" KC" if values.get(key) is not None else "")
        for label, key in EXTRAS
    )
    embed.add_field(name="\u200b", value=solo_rows + "\n\u200b", inline=False)
    embed.add_field(
        name="Application Status",
        value=f"{result}\n{gear_status(reviewer_id)}\nStaff approval pending\n\u200b",
        inline=False,
    )
    if not qualified:
        embed.add_field(name="Alternative Pathways", value="Raids Prep Program or Raid Trial (staff discretion).\nRaid Trial: 3 member vouches within 1-2 weeks.", inline=False)
        trial_rows = "\n".join(
            f"**{label}:** {fmt(key)}" + (" KC" if values.get(key) is not None else "")
            for label, key in TRIAL_REFERENCE
        )
        embed.add_field(name="Raid Trial Reference", value=trial_rows, inline=False)
    timestamp = payload.get("snapshot")
    if timestamp:
        try:
            timestamp = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            timestamp = timestamp.astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %I:%M %p ET").replace(", 0", ", ")
        except (ValueError, AttributeError):
            timestamp = "Unknown"
    else:
        timestamp = "Unavailable"
    footer = f"Wise Old Man • Last updated: {timestamp}"
    if test:
        footer += " • Staff test"
    if incomplete or special_unknown:
        embed.add_field(name="Data note", value="N/A = unranked or unavailable; totals may be incomplete.", inline=False)
    if note:
        embed.add_field(name="Snapshot status", value=note, inline=False)
    embed.set_footer(text=footer)
    return embed

class ApplicationView(discord.ui.View):
    def __init__(self, cog, rsn=None, verified=False, approved=False):
        super().__init__(timeout=None)
        self.cog = cog
        self.remove_item(self.verify_gear if verified else self.undo_gear)
        if not verified or approved:
            self.remove_item(self.final_approve)
        if approved:
            self.remove_item(self.refresh)
            self.remove_item(self.undo_gear if verified else self.verify_gear)
        if rsn:
            self.add_item(discord.ui.Button(label="View WOM Profile", url="https://wiseoldman.net/players/" + quote(rsn, safe="")))

    @discord.ui.button(label="Refresh Stats", style=discord.ButtonStyle.primary, custom_id="novus:application:refresh:v1")
    async def refresh(self, interaction, button):
        await self.cog.refresh(interaction)

    @discord.ui.button(label="Verify Gear", style=discord.ButtonStyle.success, custom_id="novus:application:gear:verify:v1", row=0)
    async def verify_gear(self, interaction, button):
        await self.cog.review_gear(interaction, True)

    @discord.ui.button(label="Undo Gear Verification", style=discord.ButtonStyle.secondary, custom_id="novus:application:gear:undo:v1", row=0)
    async def undo_gear(self, interaction, button):
        await self.cog.review_gear(interaction, False)

    @discord.ui.button(label="Approve Applicant", style=discord.ButtonStyle.success, custom_id="novus:application:approve:v1", row=1)
    async def final_approve(self, interaction, button):
        await self.cog.confirm_approval(interaction)

class ApprovalChoice(discord.ui.View):
    def __init__(self, cog, actor_id, message_id):
        super().__init__(timeout=120)
        self.cog = cog
        self.actor_id = actor_id
        self.message_id = message_id

    @discord.ui.select(placeholder="Confirm completed membership pathway", options=[
        discord.SelectOption(label="200+ raid KC", value="standard"),
        discord.SelectOption(label="100+ KC + Inferno/Colosseum", value="alternative"),
        discord.SelectOption(label="Raids Prep Program completed", value="rpp"),
        discord.SelectOption(label="Raid Trial completed (3 vouches)", value="raidtrial"),
    ])
    async def choose(self, interaction, select):
        if interaction.user.id != self.actor_id:
            await interaction.response.send_message("Only the initiating staff member may confirm.", ephemeral=True)
            return
        await self.cog.complete_approval(interaction, self.message_id, select.values[0])

class ApplicationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks = {}
        self.cooldowns = {}
        self.test_checked = False

    async def fetch_stats(self, rsn, update=False):
        url = "https://api.wiseoldman.net/v2/players/" + quote(rsn, safe="")
        headers = {"User-Agent": "NovusRoleSync/1.0"}
        if WOM_API_KEY:
            headers["x-api-key"] = WOM_API_KEY
        async def request(method):
            async with self.bot.http_session.request(method, url, headers=headers, timeout=20) as response:
                if response.status != 200 and response.status != 201:
                    return None, response.status
                return await response.json(), response.status
        if update:
            try:
                data, status = await request("POST")
                if data and data.get("latestSnapshot"):
                    return extract(data), None
            except Exception:
                logger.warning("WOM update request failed for application")
        data, status = await request("GET")
        if not data and status == 404 and not update:
            data, status = await request("POST")
        if not data or not data.get("latestSnapshot"):
            return None, f"WOM snapshot unavailable (HTTP {status}); request a refresh or review manually."
        return extract(data), ("Showing latest saved WOM snapshot; fresh update was not confirmed." if update else None)

    async def cache_verified(self, guild_id, discord_id, nickname):
        rsn = primary_name(nickname)
        if guild_id != GUILD or not rsn:
            return
        with closing(db()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO application_kc_cache VALUES (?, ?, ?, NULL)", (guild_id, discord_id, rsn))
        try:
            payload, _ = await self.fetch_stats(rsn)
            if payload:
                with closing(db()) as conn, conn:
                    conn.execute("UPDATE application_kc_cache SET payload=? WHERE guild_id=? AND discord_id=? AND rsn=?", (json.dumps(payload), guild_id, discord_id, rsn))
        except Exception:
            logger.exception("Application KC verification lookup failed")

    def resolve_applicant(self, channel):
        candidates = []
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Member) and not target.bot and overwrite.view_channel is True:
                candidates.append(target)
        if len(candidates) != 1:
            return None, None
        member = candidates[0]
        if not any(r.name.strip().casefold() == "verified" for r in member.roles):
            return None, None
        with closing(db()) as conn:
            saved = conn.execute("SELECT rsn FROM links WHERE guild_id=? AND discord_id=?", (channel.guild.id, member.id)).fetchone()
        rsn = primary_name(saved[0] if saved and saved[0] else member.nick)
        return member.id, rsn

    async def get_payload(self, rsn, applicant_id, update=False):
        try:
            payload, note = await self.fetch_stats(rsn, update=update)
        except Exception:
            logger.exception("Application WOM lookup failed")
            payload, note = None, "WOM could not be reached. Staff review needed."
        if payload:
            if applicant_id:
                with closing(db()) as conn, conn:
                    conn.execute("INSERT OR REPLACE INTO application_kc_cache VALUES (?, ?, ?, ?)", (GUILD, applicant_id, rsn, json.dumps(payload)))
            return payload, note
        if applicant_id:
            with closing(db()) as conn:
                cached = conn.execute("SELECT rsn, payload FROM application_kc_cache WHERE guild_id=? AND discord_id=?", (GUILD, applicant_id)).fetchone()
            if cached and cached[1] and sanitize_rsn(cached[0]).casefold() == sanitize_rsn(rsn).casefold():
                return json.loads(cached[1]), "WOM lookup failed; showing previously saved snapshot. Staff should refresh before approval."
        return None, note

    async def publish(self, channel):
        if channel.guild.id != GUILD or channel.category_id != CATEGORY:
            return
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            if get_report(channel.id):
                return
            # Avoid duplication if a send succeeded immediately before a restart.
            async for msg in channel.history(limit=30):
                if msg.author.id == self.bot.user.id and any(e.title == REPORT_TITLE for e in msg.embeds):
                    logger.warning("Application report already present without saved mapping: %s", channel.id)
                    return
            test = channel.id == TEST_CHANNEL
            if test:
                applicant_id, rsn = None, "Vecs"
            else:
                applicant_id, rsn = self.resolve_applicant(channel)
            if not rsn:
                logger.warning("Application applicant not unambiguous in channel %s; skipping report", channel.id)
                return
            payload, note = await self.get_payload(rsn, applicant_id, update=True)
            message = await channel.send(content=APPLICATION_INTRO, embed=report_embed(rsn, payload, test, note), view=ApplicationView(self, rsn), allowed_mentions=discord.AllowedMentions.none())
            save_report(channel.id, applicant_id, rsn, message.id)
            logger.info("Application KC report published channel=%s message=%s rsn=%s total=%s", channel.id, message.id, rsn, sum((payload or {}).get("values", {}).get(k) or 0 for _,k in RAIDS))

    @commands.Cog.listener()
    async def on_ready(self):
        if self.test_checked:
            return
        self.test_checked = True
        channel = self.bot.get_channel(TEST_CHANNEL)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(TEST_CHANNEL)
            except discord.HTTPException:
                logger.warning("Application test ticket unavailable")
                return
        try:
            if isinstance(channel, discord.TextChannel) and channel.guild.id == GUILD and channel.category_id == CATEGORY:
                row = get_report(channel.id)
                if row:
                    applicant_id, rsn, message_id = row
                    message = await channel.fetch_message(message_id)
                    if message.author.id == self.bot.user.id and any(e.title == REPORT_TITLE for e in message.embeds) and not final_approval(channel.id, message.id):
                        payload, note = await self.get_payload(rsn, applicant_id, update=False)
                        if payload:
                            reviewer = gear_reviewer(channel.id, message.id)
                            await message.edit(content=APPLICATION_INTRO, embed=report_embed(rsn, payload, True, note, reviewer), view=ApplicationView(self, rsn, verified=bool(reviewer)), allowed_mentions=discord.AllowedMentions.none())
                            logger.info("Restyled existing application test report channel=%s message=%s", channel.id, message.id)
                else:
                    await self.publish(channel)
                await self.test_application_messages(channel)
        except Exception:
            logger.exception("Application test report failed")

    async def test_application_messages(self, channel):
        # One-time, explicitly requested rendering test; never invokes approval or changes roles.
        if channel.guild.id != GUILD or channel.category_id != CATEGORY or channel.id != TEST_CHANNEL:
            return
        key = "vecs-intro-welcome-20261125-v1"
        marker = "**Staff test - welcome message preview (no roles changed)**\n"
        with closing(db()) as conn:
            if conn.execute("SELECT 1 FROM application_message_tests WHERE test_key=?", (key,)).fetchone():
                return
        row = get_report(channel.id)
        if not row:
            raise RuntimeError("Test report not found")
        report = await channel.fetch_message(row[2])
        if report.author.id != self.bot.user.id or not any(e.title == REPORT_TITLE for e in report.embeds):
            raise RuntimeError("Test report ownership or title mismatch")
        if report.content != APPLICATION_INTRO:
            if final_approval(channel.id, report.id):
                intro_marker = "**Staff test - application introduction preview**\n"
                async for candidate in channel.history(limit=50):
                    if candidate.author.id == self.bot.user.id and candidate.content == intro_marker + APPLICATION_INTRO:
                        break
                else:
                    await channel.send(intro_marker + APPLICATION_INTRO, allowed_mentions=discord.AllowedMentions.none())
            else:
                await report.edit(content=APPLICATION_INTRO, allowed_mentions=discord.AllowedMentions.none())
                report = await channel.fetch_message(report.id)
                if report.content != APPLICATION_INTRO:
                    raise RuntimeError("Introduction content mismatch")
        logger.info("Application message test: intro verified channel=%s report=%s", channel.id, report.id)
        expected = marker + welcome_text(channel.guild)
        preview = None
        async for candidate in channel.history(limit=50):
            if candidate.author.id == self.bot.user.id and candidate.content == expected:
                preview = candidate
                break
        if preview is None:
            preview = await channel.send(expected, allowed_mentions=discord.AllowedMentions.none())
        verified = await channel.fetch_message(preview.id)
        if verified.content != expected:
            raise RuntimeError("Welcome preview content mismatch")
        with closing(db()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO application_message_tests VALUES (?, ?)", (key, preview.id))
        logger.info("Application message test: welcome verified channel=%s message=%s; no roles changed", channel.id, preview.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            return
        await asyncio.sleep(10)
        try:
            fresh = await self.bot.fetch_channel(channel.id)
            await self.publish(fresh)
        except Exception:
            logger.exception("New application report failed for %s", channel.id)

    @commands.Cog.listener()
    async def on_message(self, message):
        channel = message.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            return
        if message.author.id == self.bot.user.id or not (message.author.bot or message.webhook_id):
            return
        if not any("application created" in ((e.title or "") + "\n" + (e.description or "")).casefold() for e in message.embeds):
            return
        await asyncio.sleep(3)
        try:
            await self.publish(await self.bot.fetch_channel(channel.id))
        except Exception:
            logger.exception("Application opening-message report failed")

    async def confirm_approval(self, interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY or not is_staff(interaction.user):
            await interaction.response.send_message("Only staff may approve applications.", ephemeral=True)
            return
        row = get_report(channel.id)
        if not row or not interaction.message or row[2] != interaction.message.id:
            await interaction.response.send_message("This report is no longer active.", ephemeral=True)
            return
        if final_approval(channel.id, row[2]) or not gear_reviewer(channel.id, row[2]):
            await interaction.response.send_message("Application already approved or gear review still pending.", ephemeral=True)
            return
        if row[0] == interaction.user.id:
            await interaction.response.send_message("Another staff member must approve your application.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Select the completed membership pathway to confirm final approval. Selecting a pathway grants Member access and posts the welcome message." + ("\n**Test ticket: no roles will change.**" if channel.id == TEST_CHANNEL else ""),
            view=ApprovalChoice(self, interaction.user.id, row[2]), ephemeral=True,
        )

    async def complete_approval(self, interaction, message_id, pathway):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY or not is_staff(interaction.user) or pathway not in PATHWAYS:
            await interaction.response.send_message("Only authorized staff may approve applications.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            row = get_report(channel.id)
            if not row or row[2] != message_id or not gear_reviewer(channel.id, message_id) or final_approval(channel.id, message_id):
                await interaction.followup.send("Approval is no longer available. Check gear verification and application status.", ephemeral=True)
                return
            applicant_id, rsn, _ = row
            test = channel.id == TEST_CHANNEL
            if applicant_id == interaction.user.id or (not applicant_id and not test):
                await interaction.followup.send("Applicant identity cannot be confirmed for approval.", ephemeral=True)
                return
            try:
                message = await channel.fetch_message(message_id)
                if message.author.id != self.bot.user.id or not message.embeds or message.embeds[0].title != REPORT_TITLE:
                    await interaction.followup.send("This report is no longer active.", ephemeral=True)
                    return
                embed = message.embeds[0].copy()
                status_index = next((i for i, field in enumerate(embed.fields) if field.name == "Application Status"), None)
                next_index = next((i for i, field in enumerate(embed.fields) if field.name == "Next Step"), None)
                if status_index is None:
                    await interaction.followup.send("Refresh the report before approval.", ephemeral=True)
                    return
                if pathway in ("standard", "alternative"):
                    payload, note = await self.get_payload(rsn, applicant_id, update=False)
                    values = (payload or {}).get("values", {})
                    total = sum(values.get(key) or 0 for _, key in RAIDS)
                    special = any((values.get(key) or 0) >= 1 for _, key in EXTRAS)
                    valid = total >= 200 if pathway == "standard" else total >= 100 and special
                    if not valid:
                        await interaction.followup.send("WOM does not confirm the selected pathway. Refresh stats or select a genuinely completed manual pathway.", ephemeral=True)
                        return
                member = None
                logged = True
                if not test:
                    member = await channel.guild.fetch_member(applicant_id)
                    current_id, current_rsn = self.resolve_applicant(channel)
                    if current_id != applicant_id or not current_rsn or sanitize_rsn(current_rsn).casefold() != sanitize_rsn(rsn).casefold():
                        await interaction.followup.send("Applicant or RSN no longer matches this report. Use manual staff review.", ephemeral=True)
                        return
                    verify = self.bot.get_cog("VerifyCog")
                    if verify is None or not hasattr(verify, "approve_member"):
                        await interaction.followup.send("Approval service unavailable. No roles changed.", ephemeral=True)
                        return
                    member, already, logged = await verify.approve_member(channel.guild, interaction.user, member)
                save_final_approval(channel.id, message_id, interaction.user.id, pathway)
                reviewer = gear_reviewer(channel.id, message_id)
                kc_result = embed.fields[status_index].value.partition("\n")[0]
                embed.set_field_at(status_index, name="Application Status", value=f"{kc_result}\n{gear_status(reviewer)}\n✅ **{'Test approval' if test else 'Staff approval'} complete** (by <@{interaction.user.id}>)\n**Pathway:** {PATHWAYS[pathway]}", inline=False)
                if next_index is not None:
                    embed.remove_field(next_index)
                if not test:
                    embed.color = discord.Color.green()
                try:
                    await message.edit(content=None, embed=embed, view=ApplicationView(self, rsn, verified=True, approved=True), allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    logger.exception("Approval saved but report edit failed")
                try:
                    prefix = "**Staff test - welcome preview (no roles changed)**\n" if test else f"{member.mention}\n"
                    await channel.send(prefix + welcome_text(channel.guild), allowed_mentions=discord.AllowedMentions(users=[member] if member else False, roles=False, everyone=False))
                    welcome_status = "Welcome message posted."
                except discord.HTTPException:
                    logger.exception("Approval saved but welcome message failed")
                    welcome_status = "Welcome message could not be posted; staff may run !welcome manually."
                logger.info("Application final approval channel=%s applicant=%s actor=%s pathway=%s test=%s", channel.id, applicant_id, interaction.user.id, pathway, test)
                await interaction.followup.send(("Test approval complete; no roles changed. " if test else "Applicant approved. ") + welcome_status + ("" if logged else " Staff approval log could not be sent."), ephemeral=True)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
            except Exception:
                logger.exception("Application approval failed for channel %s", channel.id)
                await interaction.followup.send("Approval could not finish. Check the applicant's roles and report before retrying.", ephemeral=True)

    async def review_gear(self, interaction, verified):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            await interaction.response.send_message("This action is only available in application tickets.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("Only staff may verify gear.", ephemeral=True)
            return
        row = get_report(channel.id)
        if not row or not interaction.message or interaction.message.id != row[2]:
            await interaction.response.send_message("This report is no longer active.", ephemeral=True)
            return
        applicant_id, rsn, message_id = row
        if applicant_id == interaction.user.id and channel.id != TEST_CHANNEL:
            await interaction.response.send_message("Another staff member must verify your gear.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            try:
                message = await channel.fetch_message(message_id)
                if message.author.id != self.bot.user.id or not message.embeds or message.embeds[0].title != REPORT_TITLE:
                    await interaction.followup.send("This report is no longer active.", ephemeral=True)
                    return
                embed = message.embeds[0].copy()
                status_index = next((i for i, field in enumerate(embed.fields) if field.name == "Application Status"), None)
                next_index = next((i for i, field in enumerate(embed.fields) if field.name == "Next Step"), None)
                if status_index is None:
                    await interaction.followup.send("Refresh the report before verifying gear.", ephemeral=True)
                    return
                if final_approval(channel.id, message_id):
                    await interaction.followup.send("This application has already been approved.", ephemeral=True)
                    return
                previous = gear_reviewer(channel.id, message_id)
                if bool(previous) == verified:
                    await interaction.followup.send("Gear is already verified." if verified else "Gear verification is already pending.", ephemeral=True)
                    return
                reviewer = interaction.user.id if verified else None
                status = embed.fields[status_index].value
                result, _, _ = status.partition("\n")
                embed.set_field_at(status_index, name="Application Status", value=f"{result}\n{gear_status(reviewer)}\nStaff approval pending\n\u200b", inline=False)
                if next_index is not None:
                    embed.remove_field(next_index)
                set_gear_review(channel.id, message_id, reviewer)
                try:
                    await message.edit(content=APPLICATION_INTRO, embed=embed, view=ApplicationView(self, rsn, verified=verified), allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    set_gear_review(channel.id, message_id, previous)
                    raise
                logger.info("Application gear review channel=%s staff=%s verified=%s", channel.id, interaction.user.id, verified)
                await interaction.followup.send("Gear requirement marked as met." if verified else "Gear verification returned to pending.", ephemeral=True)
            except Exception:
                logger.exception("Application gear review failed for %s", channel.id)
                await interaction.followup.send("Unable to update the gear review. Please try again.", ephemeral=True)

    async def refresh(self, interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            await interaction.response.send_message("This report is only available inside an application ticket.", ephemeral=True)
            return
        row = get_report(channel.id)
        if not row or interaction.message.id != row[2]:
            await interaction.response.send_message("This report is no longer active.", ephemeral=True)
            return
        applicant_id, rsn, _ = row
        if interaction.user.id != applicant_id and not is_staff(interaction.user):
            await interaction.response.send_message("Only the applicant or staff may refresh this report.", ephemeral=True)
            return
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with lock:
            if time.monotonic() - self.cooldowns.get(channel.id, -1000) < 60:
                await interaction.followup.send("Please wait a minute between refreshes.", ephemeral=True)
                return
            if final_approval(channel.id, interaction.message.id):
                await interaction.followup.send("This application has already been approved.", ephemeral=True)
                return
            self.cooldowns[channel.id] = time.monotonic()
            payload, note = await self.get_payload(rsn, applicant_id, update=True)
            if not payload:
                await interaction.followup.send(note or "WOM unavailable. Existing report retained.", ephemeral=True)
                return
            reviewer = gear_reviewer(channel.id, interaction.message.id)
            await interaction.message.edit(content=APPLICATION_INTRO, embed=report_embed(rsn, payload, channel.id == TEST_CHANNEL, note, reviewer), view=ApplicationView(self, rsn, verified=bool(reviewer)), allowed_mentions=discord.AllowedMentions.none())
            await interaction.followup.send("Eligibility report refreshed.", ephemeral=True)

async def setup(bot):
    initialize()
    cog = ApplicationCog(bot)
    await bot.add_cog(cog)
    bot.add_view(ApplicationView(cog))
    bot.add_view(ApplicationView(cog, verified=True))

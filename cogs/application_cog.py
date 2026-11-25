"""Read-only Novus applicant KC screening, separate from approval and tenure."""
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

def db():
    return sqlite3.connect("wom_multi.db")

def initialize():
    with closing(db()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS application_kc_cache (guild_id INTEGER, discord_id INTEGER, rsn TEXT, payload TEXT, PRIMARY KEY(guild_id, discord_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS application_kc_reports (channel_id INTEGER PRIMARY KEY, applicant_id INTEGER, rsn TEXT NOT NULL, message_id INTEGER NOT NULL)")

def get_report(channel_id):
    with closing(db()) as conn:
        return conn.execute("SELECT applicant_id, rsn, message_id FROM application_kc_reports WHERE channel_id=?", (channel_id,)).fetchone()

def save_report(channel_id, applicant_id, rsn, message_id):
    with closing(db()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO application_kc_reports VALUES (?, ?, ?, ?)", (channel_id, applicant_id, rsn, message_id))

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

def report_embed(rsn, payload, test=False, note=None):
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
        value=f"{result}\nGear verification pending\nStaff approval pending\n\u200b",
        inline=False,
    )
    if not qualified:
        embed.add_field(name="Alternative Pathways", value="Raids Prep Program or Raid Trial (staff discretion).\nRaid Trial: 3 member vouches within 1-2 weeks.", inline=False)
        trial_rows = "\n".join(
            f"**{label}:** {fmt(key)}" + (" KC" if values.get(key) is not None else "")
            for label, key in TRIAL_REFERENCE
        )
        embed.add_field(name="Raid Trial Reference", value=trial_rows, inline=False)
    embed.add_field(
        name="Next Step",
        value="Upload a screenshot of your required gear for staff review.",
        inline=False,
    )
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
    def __init__(self, cog, rsn=None):
        super().__init__(timeout=None)
        self.cog = cog
        if rsn:
            self.add_item(discord.ui.Button(label="View WOM Profile", url="https://wiseoldman.net/players/" + quote(rsn, safe="")))

    @discord.ui.button(label="Refresh Stats", style=discord.ButtonStyle.primary, custom_id="novus:application:refresh:v1")
    async def refresh(self, interaction, button):
        await self.cog.refresh(interaction)

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
            message = await channel.send(embed=report_embed(rsn, payload, test, note), view=ApplicationView(self, rsn), allowed_mentions=discord.AllowedMentions.none())
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
                    if message.author.id == self.bot.user.id and any(e.title == REPORT_TITLE for e in message.embeds):
                        payload, note = await self.get_payload(rsn, applicant_id, update=False)
                        if payload:
                            await message.edit(embed=report_embed(rsn, payload, True, note), view=ApplicationView(self, rsn))
                            logger.info("Restyled existing application test report channel=%s message=%s", channel.id, message.id)
                else:
                    await self.publish(channel)
        except Exception:
            logger.exception("Application test report failed")

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
            self.cooldowns[channel.id] = time.monotonic()
            payload, note = await self.get_payload(rsn, applicant_id, update=True)
            if not payload:
                await interaction.followup.send(note or "WOM unavailable. Existing report retained.", ephemeral=True)
                return
            await interaction.message.edit(embed=report_embed(rsn, payload, channel.id == TEST_CHANNEL, note), view=ApplicationView(self, rsn))
            await interaction.followup.send("Eligibility report refreshed.", ephemeral=True)

async def setup(bot):
    initialize()
    cog = ApplicationCog(bot)
    await bot.add_cog(cog)
    bot.add_view(ApplicationView(cog))

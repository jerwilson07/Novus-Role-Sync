"""Weekly, read-only WOM KC eligibility notices for non-Raid-Trial members."""
import asyncio
import datetime as dt
import logging
import sqlite3
from contextlib import closing
from urllib.parse import quote
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from common import WOM_API_KEY
from novus_logging import check, notice
from cogs.application_cog import GUILD, CATEGORY, TEST_CHANNEL, RAIDS, EXTRAS, APPLICATION_OPTIONS, DETAILS_TITLES, extract, primary_name, is_staff, trial_vouch_records
from cogs.vouches_cog import is_trial

logger = logging.getLogger("WOMBot")
HUB = 1544747251983843349
ET = ZoneInfo("America/New_York")
TITLE = "✦ Novus Trial Member Eligibility"

def db():
    return sqlite3.connect("wom_multi.db", timeout=5)

def initialize():
    with closing(db()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS trial_kc_pathways (
            episode_id INTEGER PRIMARY KEY, pathway TEXT NOT NULL, staff_id INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trial_kc_notifications (
            episode_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trial_kc_weeks (
            week TEXT PRIMARY KEY, checked_at TEXT NOT NULL)""")

def kc_qualification(values):
    total = sum(v for _, key in RAIDS if type(v := values.get(key)) is int and v >= 0)
    special = any(type(v := values.get(key)) is int and v >= 1 for _, key in EXTRAS)
    return total, ("200+ total raid KC" if total >= 200 else
                   "100+ raid KC + Inferno/Colosseum" if total >= 100 and special else None)

def week_key(now):
    local = now.astimezone(ET)
    monday = local.date() - dt.timedelta(days=local.weekday())
    if local.weekday() == 0 and local.time() < dt.time(17):
        monday -= dt.timedelta(days=7)
    return monday.isoformat()

def episode(member_id):
    with closing(db()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("""SELECT * FROM trial_analytics_episodes
            WHERE guild_id=? AND discord_id=? AND ended_at IS NULL""", (GUILD, member_id)).fetchone()

def saved_pathway(episode_id):
    with closing(db()) as conn:
        row = conn.execute("SELECT pathway FROM trial_kc_pathways WHERE episode_id=?", (episode_id,)).fetchone()
    return row[0] if row else None

class TrialKCEligibilityCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        initialize()
        self.weekly.start()

    def cog_unload(self):
        self.weekly.cancel()

    @app_commands.command(name="trialpathway", description="Classify a current Trial Member for KC eligibility notifications")
    @app_commands.choices(pathway=[
        app_commands.Choice(name="KC requirements", value="kc"),
        app_commands.Choice(name="Raid Trial (vouches)", value="raidtrial"),
    ])
    async def trialpathway(self, interaction: discord.Interaction, member: discord.Member, pathway: app_commands.Choice[str]):
        if interaction.guild_id != GUILD or not is_staff(interaction.user) or not is_trial(member):
            await notice(interaction, False, "Staff only: select a current Trial Member.", ephemeral=True)
            return
        row = episode(member.id)
        if row is None:
            await notice(interaction, False, "Trial tracking has not initialized for this member yet. Try again shortly.", ephemeral=True)
            return
        with closing(db()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO trial_kc_pathways VALUES (?,?,?)",
                         (row["episode_id"], pathway.value, interaction.user.id))
        await notice(interaction, False, f"{check()} {member.display_name}: {pathway.name}. No roles changed.", ephemeral=True)

    async def pathway(self, guild, member, row):
        override = saved_pathway(row["episode_id"])
        if override:
            return override
        # Only use a current, submitted application or explicit staff classification.
        # Never treat the shared Trial Member role as proof of the pathway.
        with closing(db()) as conn:
            submissions = conn.execute("""SELECT s.channel_id,s.response_id,s.submitted_at
                FROM application_form_submissions s
                LEFT JOIN application_final_approvals a ON a.channel_id=s.channel_id AND a.message_id=s.report_id
                WHERE s.applicant_id=? AND s.channel_id!=? AND a.channel_id IS NULL
                ORDER BY s.submitted_at DESC LIMIT 1""", (member.id,TEST_CHANNEL)).fetchall()
            previous = conn.execute("""SELECT 1 FROM trial_analytics_episodes WHERE guild_id=? AND discord_id=? AND episode_id<? LIMIT 1""",
                                    (GUILD,member.id,row["episode_id"])).fetchone()
        if not submissions:
            return None
        cid, mid, submitted = submissions[0]
        if previous and submitted < row["started_at"]:
            return None
        channel = guild.get_channel(cid)
        if not isinstance(channel, discord.TextChannel) or channel.category_id != CATEGORY:
            return None
        try:
            msg = await channel.fetch_message(mid)
        except discord.HTTPException:
            return None
        if msg.author.id != self.bot.user.id or not msg.embeds or msg.embeds[0].title not in DETAILS_TITLES:
            return None
        selected = next((f.value for f in msg.embeds[0].fields if f.name == "Membership Pathway"), None)
        if selected == APPLICATION_OPTIONS["raidtrial"]:
            return "raidtrial"
        if selected in {APPLICATION_OPTIONS[k] for k in ("standard","alternative","rpp","unsure")}:
            # Existing current-trial vouches are a reason to withhold an
            # automatic KC notice unless staff explicitly classifies the trial.
            vouches, _ = trial_vouch_records(member.id)
            return "kc" if not vouches else None
        return None

    async def wom_payload(self, member):
        with closing(db()) as conn:
            saved = conn.execute("SELECT rsn,wom_id FROM links WHERE guild_id=? AND discord_id=?",
                                 (GUILD,member.id)).fetchone()
        if not saved or not saved[0] or not saved[1]:
            return None
        rsn = primary_name(saved[0])
        if not rsn:
            return None
        headers = {"User-Agent":"NovusRoleSync/1.0"}
        if WOM_API_KEY:
            headers["x-api-key"] = WOM_API_KEY
        url = "https://api.wiseoldman.net/v2/players/" + quote(rsn,safe="")
        async with self.bot.http_session.get(url, headers=headers, timeout=20) as response:
            if response.status != 200:
                return None
            data = await response.json()
        if data.get("id") != saved[1] or not data.get("latestSnapshot"):
            return None
        return extract(data)

    @tasks.loop(minutes=30)
    async def weekly(self):
        await self.bot.wait_until_ready()
        guild = self.bot.get_guild(GUILD)
        if not guild:
            return
        channel = guild.get_channel(HUB)
        if not isinstance(channel,discord.TextChannel) or channel.name != "trial-member-hub" or guild.me is None:
            return
        permissions = channel.permissions_for(guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links and permissions.read_message_history):
            logger.warning("Trial KC eligibility: missing hub permissions")
            return
        now = dt.datetime.now(dt.timezone.utc)
        week = week_key(now)
        with closing(db()) as conn:
            if conn.execute("SELECT 1 FROM trial_kc_weeks WHERE week=?", (week,)).fetchone():
                return
        trial_roles = [r for r in guild.roles if r.name.strip().casefold() == "trial member"]
        if len(trial_roles) != 1:
            return
        # Recover notices if Discord accepted the message before a DB commit.
        marker = f"Novus Membership • KC eligibility • {week}"
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id != self.bot.user.id or not msg.embeds or msg.embeds[0].footer.text != marker:
                    continue
                ids = {int(m.id) for m in msg.mentions}
                with closing(db()) as conn, conn:
                    for member_id in ids:
                        current = episode(member_id)
                        if current and dt.datetime.fromisoformat(current["started_at"]) <= msg.created_at:
                            conn.execute("INSERT OR IGNORE INTO trial_kc_notifications VALUES (?,?)",(current["episode_id"],msg.id))
        except Exception:
            logger.exception("Trial KC eligibility: notice recovery failed")
            return
        eligible, unknown, excluded, unavailable = [], [], [], []
        for member in list(trial_roles[0].members):
            if member.bot or not is_trial(member):
                continue
            try:
                row = episode(member.id)
                if row is None:
                    unknown.append(member.display_name)
                    continue
                with closing(db()) as conn:
                    if conn.execute("SELECT 1 FROM trial_kc_notifications WHERE episode_id=?", (row["episode_id"],)).fetchone():
                        continue
                pathway = await self.pathway(guild,member,row)
                if pathway == "raidtrial":
                    excluded.append(member.display_name)
                    continue
                if pathway != "kc":
                    unknown.append(member.display_name)
                    continue
                payload = await self.wom_payload(member)
                if payload is None:
                    unavailable.append(member.display_name)
                    continue
                total, qualified = kc_qualification(payload["values"])
                if qualified:
                    eligible.append((member, row["episode_id"], total, qualified))
            except Exception:
                logger.exception("Trial KC eligibility lookup failed discord=%s",member.id)
                unavailable.append(member.display_name)
            await asyncio.sleep(0.5)
        # Recheck active role and episode before announcing.
        ready = []
        for member, eid, total, qualified in eligible:
            try:
                live = await guild.fetch_member(member.id)
                current = episode(member.id)
                if is_trial(live) and current and current["episode_id"] == eid and await self.pathway(guild,live,current) == "kc":
                    ready.append((live,eid,total,qualified))
            except discord.HTTPException:
                unavailable.append(member.display_name)
        try:
            for offset in range(0,len(ready),10):
                batch = ready[offset:offset+10]
                lines = [f"{check()} {m.mention} • **{total:,} raid KC** ({qualified})" for m,_,total,qualified in batch]
                embed = discord.Embed(title=TITLE, description="\n".join(lines)+"\n\nYou meet the KC requirements for full membership. [Apply for Membership](https://discord.com/channels/1532817291048124466/1537889319220551753/1565523333762785311).", color=discord.Color.green())
                embed.set_footer(text=marker)
                embed.set_thumbnail(url=(guild.icon or self.bot.user.display_avatar).url)
                msg = await channel.send(content=" ".join(m.mention for m,_,_,_ in batch), embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=[m for m,_,_,_ in batch],roles=False,everyone=False,replied_user=False))
                with closing(db()) as conn, conn:
                    conn.executemany("INSERT OR IGNORE INTO trial_kc_notifications VALUES (?,?)", [(eid,msg.id) for _,eid,_,_ in batch])
            if not unavailable:
                with closing(db()) as conn, conn:
                    conn.execute("INSERT OR IGNORE INTO trial_kc_weeks VALUES (?,?)",(week,now.isoformat()))
            logger.info("Trial KC weekly check week=%s notified=%s raidtrial_excluded=%s unclassified=%s unavailable=%s",week,len(ready),excluded,unknown,unavailable)
        except Exception:
            logger.exception("Trial KC weekly notification failed")

    @weekly.before_loop
    async def wait_ready(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(45)

async def setup(bot):
    await bot.add_cog(TrialKCEligibilityCog(bot))

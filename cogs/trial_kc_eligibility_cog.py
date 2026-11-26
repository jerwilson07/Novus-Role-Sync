"""Weekly, read-only WOM KC eligibility notices for non-Raid-Trial members."""
import asyncio
import datetime as dt
import calendar
import json
import time
import logging
import sqlite3
from contextlib import closing
from urllib.parse import quote
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from common import WOM_API_KEY, sanitize_rsn
from rpp_progress import RPP_KC, rpp_progress
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
        conn.execute("""CREATE TABLE IF NOT EXISTS trial_kc_start_dates (
            episode_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, staff_id INTEGER NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trial_raid_command_events (
            message_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
            discord_id INTEGER NOT NULL, staff_id INTEGER NOT NULL,
            sent_at TEXT NOT NULL, episode_id INTEGER)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS trial_kc_weeks (
            week TEXT PRIMARY KEY, checked_at TEXT NOT NULL)""")

def kc_qualification(values):
    total = sum(v for _, key in RAIDS if type(v := values.get(key)) is int and v >= 0)
    special = any(type(v := values.get(key)) is int and v >= 1 for _, key in EXTRAS)
    return total, ("200+ total raid KC" if total >= 200 else
                   "100+ raid KC + Inferno/Colosseum" if total >= 100 and special else None)

def trial_deadline(started, pathway):
    if started is None or pathway not in ("kc", "rpp", "raidtrial"):
        return None
    local = started.astimezone(ET)
    if pathway == "raidtrial":
        return local + dt.timedelta(days=14)
    month = local.month % 12 + 1
    year = local.year + (local.month == 12)
    return local.replace(year=year, month=month,
                         day=min(local.day, calendar.monthrange(year, month)[1]))

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
        self._audit_cache = None
        self._preview_done = False
        self.weekly.start()

    def cog_unload(self):
        self.weekly.cancel()

    @app_commands.command(name="trialpathway", description="Classify a current Trial Member for KC eligibility notifications")
    @app_commands.choices(pathway=[
        app_commands.Choice(name="KC requirements", value="kc"),
        app_commands.Choice(name="Raids Prep Program", value="rpp"),
        app_commands.Choice(name="Raid Trial (vouches)", value="raidtrial"),
    ])
    @app_commands.describe(start_date="Optional verified Trial Member role assignment date (YYYY-MM-DD, Eastern)")
    async def trialpathway(self, interaction: discord.Interaction, member: discord.Member, pathway: app_commands.Choice[str], start_date: str = ""):
        if interaction.guild_id != GUILD or not is_staff(interaction.user) or not is_trial(member):
            await notice(interaction, False, "Staff only: select a current Trial Member.", ephemeral=True)
            return
        row = episode(member.id)
        if row is None:
            await notice(interaction, False, "Trial tracking has not initialized for this member yet. Try again shortly.", ephemeral=True)
            return
        started = None
        if start_date:
            try:
                started = dt.datetime.combine(dt.date.fromisoformat(start_date), dt.time(), tzinfo=ET)
                if started > dt.datetime.now(ET) or started < member.joined_at.astimezone(ET).replace(hour=0,minute=0,second=0,microsecond=0):
                    raise ValueError
            except (TypeError, ValueError, AttributeError):
                await notice(interaction, False, "Use a verified role assignment date (YYYY-MM-DD) within this member's server membership.", ephemeral=True)
                return
        with closing(db()) as conn, conn:
            if started:
                conn.execute("INSERT OR REPLACE INTO trial_kc_start_dates VALUES (?,?,?)",
                             (row["episode_id"], started.isoformat(), interaction.user.id))
            conn.execute("INSERT OR REPLACE INTO trial_kc_pathways VALUES (?,?,?)",
                         (row["episode_id"], pathway.value, interaction.user.id))
        await notice(interaction, False, f"{check()} {member.display_name}: {pathway.name}. No roles changed.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        # A staff-issued command inside a real application ticket is a
        # pathway decision, not a generic mention of the command elsewhere.
        channel = message.channel
        if (not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD
                or channel.category_id != CATEGORY or channel.id == TEST_CHANNEL
                or message.author.bot or not is_staff(message.author)
                or message.content.strip().casefold() != "!raidtrial"):
            return
        application = self.bot.get_cog("ApplicationCog")
        if application is None:
            return
        try:
            applicant_id, _ = await application.resolve_applicant(channel)
            if not applicant_id:
                logger.warning("Raid Trial command not classified: applicant unverified channel=%s", channel.id)
                return
            with closing(db()) as conn, conn:
                conn.execute("""INSERT OR IGNORE INTO trial_raid_command_events
                    (message_id,channel_id,discord_id,staff_id,sent_at)
                    VALUES (?,?,?,?,?)""",
                    (message.id,channel.id,applicant_id,message.author.id,message.created_at.isoformat()))
            member = channel.guild.get_member(applicant_id)
            row = episode(applicant_id) if member and is_trial(member) else None
            if row:
                await self.command_pathway(channel.guild, member, row)
            logger.info("Raid Trial command recorded applicant=%s ticket=%s message=%s", applicant_id,channel.id,message.id)
        except Exception:
            logger.exception("Raid Trial command tracking failed channel=%s",channel.id)

    async def command_pathway(self, guild, member, row):
        started = await self.verified_start(guild,member,row)
        if started is None:
            return None
        with closing(db()) as conn:
            events = conn.execute("""SELECT message_id, staff_id, sent_at
                FROM trial_raid_command_events
                WHERE discord_id=? AND (episode_id IS NULL OR episode_id=?)
                ORDER BY sent_at DESC""",(member.id,row["episode_id"])).fetchall()
            previous = conn.execute("""SELECT MAX(ended_at) FROM trial_analytics_episodes
                WHERE guild_id=? AND discord_id=? AND episode_id<?""",
                (GUILD,member.id,row["episode_id"])).fetchone()[0]
        cutoff = started - dt.timedelta(days=14)
        if previous:
            cutoff = max(cutoff,dt.datetime.fromisoformat(previous))
        for mid, staff_id, sent_at in events:
            stamp = dt.datetime.fromisoformat(sent_at)
            if stamp < cutoff or stamp > dt.datetime.now(dt.timezone.utc):
                continue
            with closing(db()) as conn, conn:
                conn.execute("UPDATE trial_raid_command_events SET episode_id=? WHERE message_id=? AND (episode_id IS NULL OR episode_id=?)",
                             (row["episode_id"],mid,row["episode_id"]))
                conn.execute("INSERT OR IGNORE INTO trial_kc_pathways VALUES (?,?,?)",
                             (row["episode_id"],"raidtrial",staff_id))
            return saved_pathway(row["episode_id"])
        return None

    async def pathway(self, guild, member, row):
        override = saved_pathway(row["episode_id"])
        if override:
            return override
        command = await self.command_pathway(guild,member,row)
        if command:
            return command
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
            return ("rpp" if selected == APPLICATION_OPTIONS["rpp"] else "kc") if not vouches else None
        return None

    async def classify_known_raid_trial(self, guild):
        # Staff identified this current trial by RSN. Bind only once to a
        # unique verified link and active episode, never to future trials.
        with closing(db()) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS trial_kc_seeded (key TEXT PRIMARY KEY)")
            if conn.execute("SELECT 1 FROM trial_kc_seeded WHERE key='drysimulatr-20261126'").fetchone():
                return
            links = conn.execute("SELECT discord_id,rsn,wom_id FROM links WHERE guild_id=?", (GUILD,)).fetchall()
        matches = [(did,wid) for did,rsn,wid in links if wid and primary_name(rsn) and sanitize_rsn(primary_name(rsn)).casefold()=="drysimulatr"]
        if len(matches)!=1:
            logger.info("Drysimulatr Raid Trial classification pending unique linked RSN")
            return
        member = guild.get_member(matches[0][0])
        row = episode(member.id) if member and is_trial(member) else None
        if not row:
            return
        with closing(db()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO trial_kc_pathways VALUES (?,?,?)",(row["episode_id"],"raidtrial",0))
            conn.execute("INSERT OR IGNORE INTO trial_kc_seeded VALUES ('drysimulatr-20261126')")
        logger.info("Drysimulatr Raid Trial classified discord=%s episode=%s",member.id,row["episode_id"])

    async def apply_yngasmaster_pathway_change(self, guild):
        """One staff-approved switch for the current RPP episode, not future trials."""
        key = "yngasmaster-kc-20261126"
        with closing(db()) as conn:
            if conn.execute("SELECT 1 FROM trial_kc_seeded WHERE key=?", (key,)).fetchone():
                return
            links = conn.execute("SELECT discord_id,rsn,wom_id FROM links WHERE guild_id=?", (GUILD,)).fetchall()
        matches = [(did,wid) for did,rsn,wid in links if wid and primary_name(rsn) and sanitize_rsn(primary_name(rsn)).casefold()=="yngasmaster"]
        if len(matches) != 1:
            logger.warning("Yngasmaster pathway change pending unique verified link")
            return
        member = guild.get_member(matches[0][0])
        row = episode(member.id) if member and is_trial(member) else None
        if not row:
            return
        start = await self.verified_start(guild, member, row)
        # Match the exact currently reviewed trial, including its original
        # role assignment. Do not apply this decision to a later episode.
        if not start or start.astimezone(ET).date() != dt.date(2026,11,10):
            logger.warning("Yngasmaster pathway change pending verified original trial start")
            return
        with closing(db()) as conn, conn:
            current = conn.execute("SELECT pathway FROM trial_kc_pathways WHERE episode_id=?", (row["episode_id"],)).fetchone()
            if not current or current[0] not in ("rpp","kc"):
                logger.warning("Yngasmaster pathway change skipped: current staff classification differs")
                return
            conn.execute("UPDATE trial_kc_pathways SET pathway='kc' WHERE episode_id=? AND pathway='rpp'", (row["episode_id"],))
            conn.execute("INSERT OR IGNORE INTO trial_kc_seeded VALUES (?)", (key,))
        logger.info("Yngasmaster pathway change verified episode=%s pathway=kc deadline=%s", row["episode_id"],trial_deadline(start,"kc").isoformat())

    async def verified_start(self, guild, member, row):
        with closing(db()) as conn:
            saved = conn.execute("SELECT started_at FROM trial_kc_start_dates WHERE episode_id=?", (row["episode_id"],)).fetchone()
        if saved:
            return dt.datetime.fromisoformat(saved[0])
        if row["entry_source"] == "role_event":
            return dt.datetime.fromisoformat(row["started_at"])
        # Existing trials may have been discovered late. A real Discord
        # role-add audit entry is required; observation is not assignment.
        if guild.me is None or not guild.me.guild_permissions.view_audit_log:
            return None
        if self._audit_cache is None or time.monotonic()-self._audit_cache[0]>300:
            grants = {}
            try:
                async for event in guild.audit_logs(limit=500, action=discord.AuditLogAction.member_role_update,
                        after=dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=45)):
                    target = getattr(event.target,"id",None)
                    added = getattr(event.after,"roles",())
                    if target and any(r.name.strip().casefold()=="trial member" for r in added):
                        grants.setdefault(target,event.created_at)
            except discord.HTTPException:
                return None
            self._audit_cache = (time.monotonic(),grants)
        started = self._audit_cache[1].get(member.id)
        if started and member.joined_at and started >= member.joined_at:
            return started
        return None

    async def trial_timing(self, guild, member):
        if member is None or not is_trial(member):
            return None, None, None
        row = episode(member.id)
        if not row:
            return None, None, None
        pathway = await self.pathway(guild,member,row)
        start = await self.verified_start(guild,member,row)
        return pathway, start, trial_deadline(start,pathway)

    async def wom_payload(self, member):
        # Trial eligibility is read-only and does not require in-game clan
        # membership. Prefer stable linked/episode WOM identity; otherwise
        # require a verified RSN or the current staff-confirmed exact RSN.
        row = episode(member.id)
        with closing(db()) as conn:
            saved = conn.execute("SELECT rsn,wom_id FROM links WHERE guild_id=? AND discord_id=?",
                                 (GUILD,member.id)).fetchone()
            cached = conn.execute("SELECT rsn FROM application_kc_cache WHERE guild_id=? AND discord_id=?",
                                  (GUILD,member.id)).fetchone()
        rsn = primary_name(saved[0]) if saved and saved[0] else None
        expected = saved[1] if saved else None
        if row and row["wom_id"] is not None:
            if expected is not None and expected != row["wom_id"]:
                return None
            expected = row["wom_id"]
            rsn = rsn or primary_name(row["rsn"])
        if not rsn and any(r.name.strip().casefold()=="verified" for r in member.roles):
            rsn = primary_name(cached[0]) if cached else None
            rsn = rsn or primary_name(member.nick)
        # Staff confirmed this specific current trial's exact in-game name.
        # This is not a membership link and grants no roles or clan access.
        if not rsn and member.display_name.casefold()=="sirgrotesque" and row and is_trial(member):
            start = await self.verified_start(member.guild,member,row)
            if start and start.astimezone(ET).date()==dt.date(2026,11,14):
                rsn = "sirgrotesque"
        if not rsn:
            return None
        headers = {"User-Agent":"NovusRoleSync/1.0"}
        if WOM_API_KEY:
            headers["x-api-key"] = WOM_API_KEY
        url = "https://api.wiseoldman.net/v2/players/" + quote(rsn,safe="")
        async with self.bot.http_session.get(url, headers=headers, timeout=20) as response:
            if response.status != 200:
                logger.info("Trial WOM profile unavailable rsn=%s HTTP=%s",rsn,response.status)
                return None
            data = await response.json()
        if expected is not None and data.get("id") != expected:
            return None
        if not any(isinstance(n,str) and sanitize_rsn(n).casefold()==sanitize_rsn(rsn).casefold()
                   for n in (data.get("username"),data.get("displayName"))):
            return None
        if not data.get("latestSnapshot"):
            return None
        result = extract(data)
        bosses = (data['latestSnapshot'].get('data') or {}).get('bosses') or {}
        for _,key,_,_ in RPP_KC:
            kc = (bosses.get(key) or {}).get('kills')
            result['values'][key] = kc if type(kc) is int and kc >= 0 else None
        return result

    async def rpp_member_progress(self, payload):
        """Use the WOM-verified account name and shared rate-limited API client."""
        payload = payload or {}
        rp = self.bot.get_cog("RuneProfileCog")
        name = payload.get("name")
        tasks = await rp.task_data(name) if rp and name else None
        return rpp_progress(payload.get("values"), tasks)

    async def preview_progress(self, guild):
        """Read-only staff-requested preview. No Discord messages or role changes."""
        trial_roles = [r for r in guild.roles if r.name.strip().casefold() == "trial member"]
        if len(trial_roles) != 1:
            logger.warning("Trial progress preview: trial role unavailable")
            return
        entries = []
        for member in list(trial_roles[0].members):
            if member.bot or not is_trial(member):
                continue
            entry = {"name":member.display_name}
            try:
                row = episode(member.id)
                pathway, start, deadline = await self.trial_timing(guild,member)
                entry.update(pathway=pathway, start=start.isoformat() if start else None,
                             deadline=deadline.isoformat() if deadline else None)
                if deadline:
                    entry["days_remaining"] = (deadline.date()-dt.datetime.now(ET).date()).days
                if pathway == "raidtrial":
                    rows, legacy = trial_vouch_records(member.id)
                    entry["vouches"] = len(rows)
                    entry["vouches_legacy"] = legacy
                    if start:
                        from cogs.vouches_cog import get_vouches
                        entry["vouches"] = sum(1 for record in get_vouches(GUILD,member.id)
                            if start <= dt.datetime.fromisoformat(record[3].replace("Z","+00:00")) <= deadline)
                        entry["vouches_legacy"] = False
                elif pathway in ("kc","rpp"):
                    payload = await self.wom_payload(member)
                    if payload:
                        values = payload["values"]
                        total, qualification = kc_qualification(values)
                        entry.update(raid_kc=total, qualification=qualification, snapshot=payload.get("snapshot"),
                                     inferno=values.get("tzkal_zuk"), colosseum=values.get("sol_heredit"),
                                     raid_values={key:values.get(key) for _,key in RAIDS})
                    else:
                        entry["wom_unavailable"] = True
                if pathway=="rpp":
                    entry["rpp_progress"]=await self.rpp_member_progress(payload)
            except Exception:
                logger.exception("Trial progress preview failed member=%s",member.id)
                entry["error"] = True
            entries.append(entry)
        logger.info("NOVUS_TRIAL_PROGRESS_PREVIEW %s",json.dumps(entries,ensure_ascii=False))

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
        await self.classify_known_raid_trial(guild)
        await self.apply_yngasmaster_pathway_change(guild)
        if not self._preview_done and dt.datetime.now(ET).date() == dt.date(2026,11,26):
            self._preview_done = True
            await self.preview_progress(guild)
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
                if pathway not in ("kc","rpp"):
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
                if is_trial(live) and current and current["episode_id"] == eid and await self.pathway(guild,live,current) in ("kc","rpp"):
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

"""Read-only longitudinal PvM trial analytics. Never grants or removes roles."""
import asyncio
import datetime as dt
import json
import logging
import sqlite3
from contextlib import closing
from urllib.parse import quote

import discord
from discord.ext import commands, tasks

from common import WOM_API_KEY, sanitize_rsn
from cogs.application_cog import GUILD, primary_name, is_staff

logger = logging.getLogger("WOMBot")
DB = "wom_multi.db"

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def db():
    return sqlite3.connect(DB, timeout=15)

def init():
    with closing(db()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS trial_analytics_episodes (
            episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL, discord_id INTEGER NOT NULL,
            rsn TEXT, wom_id INTEGER, started_at TEXT NOT NULL,
            entry_source TEXT NOT NULL, baseline_json TEXT,
            snapshot_at TEXT, captured_at TEXT,
            ended_at TEXT, outcome TEXT, outcome_source TEXT,
            vouch_count INTEGER NOT NULL DEFAULT 0,
            first_three_at TEXT
        )""")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS trial_analytics_one_active
            ON trial_analytics_episodes(guild_id, discord_id) WHERE ended_at IS NULL""")

def roles(member):
    return {r.name.strip().casefold() for r in member.roles}

def active(member_id):
    with closing(db()) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM trial_analytics_episodes WHERE guild_id=? AND discord_id=? AND ended_at IS NULL", (GUILD, member_id)).fetchone()

def active_ids():
    with closing(db()) as conn:
        return [r[0] for r in conn.execute("SELECT discord_id FROM trial_analytics_episodes WHERE guild_id=? AND ended_at IS NULL", (GUILD,))]

def begin(member_id, rsn, wom_id, source):
    with closing(db()) as conn, conn:
        conn.execute("INSERT OR IGNORE INTO trial_analytics_episodes(guild_id,discord_id,rsn,wom_id,started_at,entry_source) VALUES (?,?,?,?,?,?)", (GUILD,member_id,rsn,wom_id,now(),source))
    return active(member_id)

def vouch_metrics(row):
    with closing(db()) as conn:
        records = conn.execute("SELECT voucher_id, created_at FROM trial_vouches WHERE guild_id=? AND trial_member_id=? ORDER BY created_at", (GUILD,row["discord_id"])).fetchall()
    start = dt.datetime.fromisoformat(row["started_at"])
    valid = {}
    for voucher, stamp in records:
        try:
            date = dt.datetime.fromisoformat(stamp.replace("Z","+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=dt.timezone.utc)
            if date >= start:
                valid.setdefault(voucher,date)
        except (TypeError, ValueError):
            continue
    times = sorted(valid.values())
    return len(times), times[2].isoformat() if len(times)>=3 else None

def record_progress(row):
    count, third = vouch_metrics(row)
    with closing(db()) as conn, conn:
        conn.execute("UPDATE trial_analytics_episodes SET vouch_count=?, first_three_at=? WHERE episode_id=? AND ended_at IS NULL", (count,third,row["episode_id"]))
    return count

def close_episode(row, outcome, source):
    count, third = vouch_metrics(row)
    if outcome == "promoted":
        outcome = "passed" if count >= 3 else "promoted_unconfirmed"
    with closing(db()) as conn, conn:
        conn.execute("UPDATE trial_analytics_episodes SET ended_at=?, outcome=?, outcome_source=?, vouch_count=?, first_three_at=? WHERE episode_id=? AND ended_at IS NULL", (now(),outcome,source,count,third,row["episode_id"]))
    logger.info("Trial analytics closed episode=%s discord=%s outcome=%s vouches=%s", row["episode_id"],row["discord_id"],outcome,count)

class TrialAnalyticsCog(commands.Cog):
    def __init__(self, bot):
        self.bot=bot
        self.locks={}
        self.last_summary = None
        self.reconcile.start()

    def cog_unload(self):
        self.reconcile.cancel()

    def lock(self, member_id):
        return self.locks.setdefault(member_id, asyncio.Lock())

    def account(self, member):
        with closing(db()) as conn:
            saved=conn.execute("SELECT rsn,wom_id FROM links WHERE guild_id=? AND discord_id=?", (GUILD,member.id)).fetchone()
        if saved and saved[0]:
            return primary_name(saved[0]), saved[1]
        if "verified" in roles(member):
            return primary_name(member.nick), None
        return None,None

    async def capture(self, row, member):
        if row["baseline_json"]:
            return
        rsn,wom_id=self.account(member)
        if not rsn:
            return
        # Never substitute another account for a frozen baseline identity.
        if row["wom_id"] is not None and wom_id is not None and row["wom_id"] != wom_id:
            return
        if row["rsn"] and sanitize_rsn(row["rsn"]).casefold()!=sanitize_rsn(rsn).casefold() and not (row["wom_id"] is not None and row["wom_id"]==wom_id):
            return
        headers={"User-Agent":"NovusRoleSync/1.0"}
        if WOM_API_KEY:
            headers["x-api-key"]=WOM_API_KEY
        url="https://api.wiseoldman.net/v2/players/"+quote(rsn,safe="")
        # For a live entry, attempt a fresh WOM update. Existing trials use the
        # latest saved snapshot and remain marked as late observations.
        data=None
        if row["entry_source"] == "role_event":
            try:
                async with self.bot.http_session.post(url,headers=headers,timeout=20) as response:
                    if response.status in (200,201):
                        data=await response.json()
            except Exception:
                logger.warning("Trial baseline fresh update unavailable episode=%s",row["episode_id"])
        try:
            if not data or not data.get("latestSnapshot"):
                async with self.bot.http_session.get(url,headers=headers,timeout=15) as response:
                    if response.status==200:
                        data=await response.json()
            if not data or not data.get("latestSnapshot"):
                return
        except Exception:
            logger.exception("Trial baseline lookup failed episode=%s",row["episode_id"])
            return
        if not data:
            return
        player_id=data.get("id")
        if wom_id is not None and player_id != wom_id:
            logger.warning("Trial baseline WOM identity mismatch episode=%s",row["episode_id"])
            return
        snap=data.get("latestSnapshot") or {}
        bosses=(snap.get("data") or {}).get("bosses") or {}
        if not bosses:
            return
        # Store ALL boss metrics, including missing/unranked separately from zero.
        baseline={key:(metric.get("kills") if type(metric.get("kills")) is int and metric["kills"]>=0 else None) for key,metric in bosses.items() if isinstance(metric,dict)}
        captured=now()
        with closing(db()) as conn, conn:
            conn.execute("""UPDATE trial_analytics_episodes SET rsn=?,wom_id=?,baseline_json=?,snapshot_at=?,captured_at=?
                WHERE episode_id=? AND baseline_json IS NULL AND ended_at IS NULL""",
                (rsn,player_id,json.dumps(baseline,sort_keys=True),snap.get("createdAt"),captured,row["episode_id"]))
        logger.info("Trial analytics baseline episode=%s source=%s bosses=%s snapshot=%s",row["episode_id"],row["entry_source"],len(baseline),snap.get("createdAt"))

    async def observe(self, member, source):
        if member.guild.id!=GUILD or member.bot:
            return
        async with self.lock(member.id):
            row=active(member.id)
            current=roles(member)
            if "trial member" in current:
                if row is None:
                    rsn,wom_id=self.account(member)
                    row=begin(member.id,rsn,wom_id,source)
                    logger.info("Trial analytics started episode=%s discord=%s source=%s",row["episode_id"],member.id,source)
                record_progress(row)
                if not row["baseline_json"]:
                    await self.capture(row,member)
            elif row:
                close_episode(row,"promoted" if "member" in current else "role_removed_unknown",source)

    @commands.Cog.listener()
    async def on_member_update(self,before,after):
        if after.guild.id!=GUILD or after.bot:
            return
        previous,current=roles(before),roles(after)
        if ("trial member" in previous) != ("trial member" in current):
            try:
                await self.observe(after,"role_event")
            except Exception:
                logger.exception("Trial analytics role event failed discord=%s",after.id)

    @commands.Cog.listener()
    async def on_member_remove(self,member):
        if member.guild.id!=GUILD:
            return
        async with self.lock(member.id):
            row=active(member.id)
            if row:
                close_episode(row,"left_server","member_remove")

    @tasks.loop(minutes=5)
    async def reconcile(self):
        guild=self.bot.get_guild(GUILD)
        if not guild:
            return
        trial_roles=[r for r in guild.roles if r.name.strip().casefold()=="trial member"]
        if len(trial_roles)!=1:
            return
        members={m.id:m for m in trial_roles[0].members if not m.bot}
        for member in members.values():
            try:
                await self.observe(member,"late_observation")
            except Exception:
                logger.exception("Trial analytics reconcile failed discord=%s",member.id)
        for member_id in active_ids():
            if member_id in members:
                continue
            try:
                member=await guild.fetch_member(member_id)
            except discord.NotFound:
                async with self.lock(member_id):
                    row=active(member_id)
                    if row:
                        close_episode(row,"left_server","reconcile")
                continue
            except discord.HTTPException:
                continue
            try:
                await self.observe(member,"reconcile")
            except Exception:
                logger.exception("Trial analytics outcome reconcile failed discord=%s",member_id)

        with closing(db()) as conn:
            counts=tuple(conn.execute("""SELECT COUNT(*), COALESCE(SUM(baseline_json IS NOT NULL),0),
                COALESCE(SUM(ended_at IS NULL),0), COALESCE(SUM(outcome='passed'),0),
                COALESCE(SUM(entry_source='late_observation'),0)
                FROM trial_analytics_episodes WHERE guild_id=?""",(GUILD,)).fetchone())
        if counts != self.last_summary:
            self.last_summary=counts
            logger.info("Trial analytics totals episodes=%s baselines=%s active=%s passed=%s late=%s",*counts)



    @commands.Cog.listener()
    async def on_ready(self):
        # Edit the existing council notice only. Never send a duplicate.
        if getattr(self, "_council_notice_running", False):
            return
        self._council_notice_running = True
        try:
            guild = self.bot.get_guild(GUILD)
            if guild is None:
                return
            channels = [c for c in guild.text_channels if c.name.casefold() == "council-chat"]
            if len(channels) != 1 or channels[0].permissions_for(guild.default_role).view_channel:
                logger.warning("Trial analytics council notice edit skipped: private channel not uniquely identified")
                return
            channel = channels[0]
            old = "## Raid Trial Analytics\n\nNovus Sync is now collecting baseline PvM statistics for Raid Trial members and tracking their vouches, trial duration and eventual outcomes. It captures all available WOM boss KCs at entry, rather than after the trial is completed.\n\n**My hypothesis:** Higher Doom and ToB/HMT KC will predict more successful trial outcomes. Doom tests individual mechanics and consistency, while ToB tests team coordination and raid execution. Experience in both may be particularly valuable.\n\nOnce we have enough completed trials, we can compare successful and unsuccessful outcomes, account for overall PvM experience, and identify which bosses are actually predictive of success. Withdrawals and unclear outcomes will be treated separately.\n\nThe goal is to eventually develop a data-driven Raid Trial recommendation to assist staff, not replace staff discretion or change our current requirements.\n\n-# Data collection is already live: 5 existing trials, with 72 boss metrics captured per player. Existing trial snapshots are flagged as late baselines."
            body = "## Raid Trial Analytics\n\nNovus Sync is now tracking Raid Trial members' boss KCs at entry, vouches, trial duration and outcomes.\n\nOver time, we'll analyze which PvM achievements (particularly Doom and ToB/HMT KC) are associated with successful trials and use the data to help assess future applicants.\n\nThis is intended to support staff discretion, not change our membership requirements.\n\n-# Data collection is live: 5 existing trials with 72 boss metrics per player. Existing trials are marked as late baselines."
            async for candidate in channel.history(limit=200):
                if candidate.author.id == self.bot.user.id and candidate.content in (old, body):
                    if candidate.content == old:
                        await candidate.edit(content=body, allowed_mentions=discord.AllowedMentions.none())
                        logger.info("Trial analytics council notice edited channel=%s message=%s",channel.id,candidate.id)
                    else:
                        logger.info("Trial analytics council notice revision already present channel=%s message=%s",channel.id,candidate.id)
                    return
            logger.warning("Trial analytics original council notice not found; no duplicate sent")
        except Exception:
            logger.exception("Trial analytics council notice edit failed")
        finally:
            self._council_notice_running = False

    @reconcile.before_loop
    async def before_reconcile(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)

async def setup(bot):
    init()
    await bot.add_cog(TrialAnalyticsCog(bot))

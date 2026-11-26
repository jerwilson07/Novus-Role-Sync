"""Read-only RuneProfile audit for active Novus members; never grants CA roles."""
import asyncio
import datetime as dt
import json
import logging
import os
import sqlite3
from contextlib import closing
from urllib.parse import quote
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from common import has_novus_member_role, find_primary_wom_membership, sanitize_rsn
from cogs.tasks_cog import has_verifiable_wom_roster, NOVUS_TRIAL_WOM_RANKS
from cogs.application_cog import GUILD, primary_name
from novus_logging import check

logger = logging.getLogger("WOMBot")
BASE = "https://api.runeprofile.com/v1"
ET = ZoneInfo("America/New_York")
MOD_CHAT = 1532835276567613531
MASTER = 1537939642366828574
GRANDMASTER = 1537939595977695315
# Anonymous API limit is 30/minute. Keep the entire audit below 24/minute.
REQUEST_INTERVAL = 2.6
TITLE = "✦ Novus Combat Achievement Promotions"

def db():
    return sqlite3.connect("wom_multi.db", timeout=5)

def init():
    with closing(db()) as conn, conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS runeprofile_snapshots (
            wom_id INTEGER PRIMARY KEY, rsn TEXT NOT NULL, checked_at TEXT NOT NULL,
            profile_updated_at TEXT, tier TEXT, points INTEGER,
            collection_obtained INTEGER, collection_total INTEGER, status TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS runeprofile_ca_notices (
            wom_id INTEGER NOT NULL, tier TEXT NOT NULL, message_id INTEGER NOT NULL,
            PRIMARY KEY(wom_id,tier))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS runeprofile_audit_days (
            day TEXT PRIMARY KEY, finished_at TEXT NOT NULL)""")

def candidate_tier(tier, role_ids):
    if GRANDMASTER in role_ids:
        return None
    if tier == "Grandmaster":
        return tier
    if tier == "Master" and MASTER not in role_ids:
        return tier
    return None

def parse_tasks(payload):
    if not isinstance(payload,dict) or not isinstance(payload.get("data"),list) or not payload["data"]:
        return None
    points = payload.get("totalPoints")
    tier = payload.get("tierReached")
    if type(points) is not int or points < 0 or tier not in (None,"Easy","Medium","Hard","Elite","Master","Grandmaster"):
        return None
    # An all-false/zero endpoint may be a profile without task-level sync.
    # Treat it as unknown, never as evidence for a demotion.
    completed = [t for t in payload["data"] if isinstance(t,dict) and t.get("completed") is True and t.get("exempt") is False]
    if not completed or points <= 0:
        return None
    return tier,points

class RuneProfileCog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.lock=asyncio.Lock()
        init()
        self.audit.start()

    def cog_unload(self):
        self.audit.cancel()

    async def request(self,rsn,suffix=""):
        headers={"User-Agent":"NovusRoleSync/1.0 (Novus OSRS clan)"}
        key=os.getenv("RUNEPROFILE_API_KEY")
        if key:
            headers["X-API-Key"]=key
        async with self.lock:
            try:
                async with self.bot.http_session.get(BASE+"/accounts/"+quote(rsn,safe="")+suffix,headers=headers,timeout=20) as response:
                    if response.status==200:
                        return await response.json(),200
                    return None,response.status
            except (asyncio.TimeoutError, OSError):
                return None,0
            finally:
                await asyncio.sleep(REQUEST_INTERVAL)

    async def task_data(self,rsn):
        # For future RPP matching: only explicit synced task completion.
        # Never assume a missing/exempt task counts as passed.
        data,status=await self.request(rsn,"/combat-achievements/tasks")
        return data if status==200 and parse_tasks(data) else None

    async def audit_once(self,guild,day):
        sync=self.bot.get_cog("TasksCog")
        channel=guild.get_channel(MOD_CHAT)
        if not sync or not isinstance(channel,discord.TextChannel) or channel.name!="mod-chat" or not guild.me:
            return
        if channel.permissions_for(guild.default_role).view_channel:
            return
        perms=channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links and perms.read_message_history):
            return
        master_role, gm_role = guild.get_role(MASTER), guild.get_role(GRANDMASTER)
        if not master_role or not gm_role or master_role.name.casefold() != "master ca" or gm_role.name.casefold() != "grandmaster ca":
            logger.warning("RuneProfile audit skipped: configured CA roles not found")
            return
        group,status=await sync.get_wom_group(25802)
        if status!=200 or not has_verifiable_wom_roster(group) or group.get("id")!=25802:
            return
        if not guild.chunked:
            await guild.chunk(cache=True)
        if not guild.chunked:
            return
        with closing(db()) as conn:
            links=conn.execute("SELECT discord_id,rsn,wom_id FROM links WHERE guild_id=?",(GUILD,)).fetchall()
        candidates=[]
        coverage={"tracked":0,"missing":0,"unsynced":0,"failed":0}
        now=dt.datetime.now(dt.timezone.utc).isoformat()
        for did,rsn,wid in links:
            member=guild.get_member(did)
            if not member or member.bot or not wid or not has_novus_member_role(member):
                continue
            primary=find_primary_wom_membership(rsn,wid,group["memberships"])
            if not primary or primary.get("role","").casefold() in NOVUS_TRIAL_WOM_RANKS:
                continue
            player=primary["player"]
            name=primary_name(player.get("displayName") or player.get("username"))
            if not name:
                continue
            try:
                summary,status=await self.request(name)
                if status==429:
                    logger.warning("RuneProfile audit paused by provider rate limit")
                    return
                if status!=200 or not isinstance(summary,dict):
                    coverage["missing" if status==404 else "failed"]+=1
                    continue
                if not isinstance(summary.get("username"),str) or sanitize_rsn(summary["username"]).casefold()!=sanitize_rsn(name).casefold():
                    coverage["failed"]+=1
                    continue
                # Positive tier evidence comes from the provider's account-
                # adjusted points-based tierReached, not per-tier task counts.
                tasks_payload,status=await self.request(name,"/combat-achievements/tasks")
                if status==429:
                    return
                if status!=200:
                    coverage["failed"]+=1
                    continue
                result=parse_tasks(tasks_payload)
                tier,points=result if result else (None,None)
                coverage["tracked" if result else "unsynced"]+=1
                clog=summary.get("collectionLog") or {}
                obtained=clog.get("obtained")
                total=clog.get("total")
                if not (type(obtained) is int and type(total) is int and 0<=obtained<=total and total>0):
                    obtained=total=None
                with closing(db()) as conn,conn:
                    conn.execute("INSERT OR REPLACE INTO runeprofile_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                        (wid,name,now,summary.get("updatedAt"),tier,points,obtained,total,"tracked" if result else "ca_unverified"))
                target=candidate_tier(tier,{r.id for r in member.roles})
                if target:
                    with closing(db()) as conn:
                        seen=conn.execute("SELECT 1 FROM runeprofile_ca_notices WHERE wom_id=? AND tier=?",(wid,target)).fetchone()
                    if not seen:
                        candidates.append((did,wid,name,target,points,summary.get("updatedAt")))
            except Exception:
                coverage["failed"]+=1
                logger.exception("RuneProfile audit lookup failed wom_id=%s",wid)
        # Cache evidence only; the existing noon staff report handles alerts.
        with closing(db()) as conn,conn:
            conn.execute("INSERT OR REPLACE INTO runeprofile_audit_days VALUES (?,?)",(day,dt.datetime.now(dt.timezone.utc).isoformat()))
        logger.info("RuneProfile daily audit day=%s coverage=%s candidates=%s",day,coverage,len(candidates))

    def promotion_report(self,guild,memberships):
        """Pending promotions from today's completed audit, rechecked against live roles.
        No role mutations or standalone posts; candidates remain actionable until granted.
        """
        day=dt.datetime.now(ET).date().isoformat()
        master_role,gm_role=guild.get_role(MASTER),guild.get_role(GRANDMASTER)
        if not master_role or not gm_role or master_role.name.casefold()!="master ca" or gm_role.name.casefold()!="grandmaster ca":
            return [],"CA role configuration unverified."
        with closing(db()) as conn:
            done=conn.execute("SELECT finished_at FROM runeprofile_audit_days WHERE day=?",(day,)).fetchone()
            if not done:
                return [],"Today's RuneProfile audit has not completed."
            rows=conn.execute("""SELECT l.discord_id,l.rsn,l.wom_id,s.rsn,s.tier,s.points,s.checked_at
                FROM links l JOIN runeprofile_snapshots s ON l.wom_id=s.wom_id
                WHERE l.guild_id=? AND s.status='tracked'""",(GUILD,)).fetchall()
        candidates=[]
        tracked=0
        for did,rsn,wid,snapshot_rsn,tier,points,checked in rows:
            try:
                if dt.datetime.fromisoformat(checked).astimezone(ET).date().isoformat()!=day:
                    continue
            except (ValueError,TypeError):
                continue
            member=guild.get_member(did)
            if not member or member.bot or not has_novus_member_role(member):
                continue
            primary=find_primary_wom_membership(rsn,wid,memberships)
            if not primary or primary.get("role","").casefold() in NOVUS_TRIAL_WOM_RANKS:
                continue
            name=primary_name(primary["player"].get("displayName") or primary["player"].get("username"))
            if not name or sanitize_rsn(name).casefold()!=sanitize_rsn(snapshot_rsn).casefold():
                continue
            tracked+=1
            target=candidate_tier(tier,{r.id for r in member.roles})
            if target and type(points) is int and points>0:
                candidates.append((member.id,target,points))
        candidates.sort(key=lambda item:(item[1]!="Grandmaster", -item[2],item[0]))
        lines=[f"{check()} <@{did}> • **{tier} CA** • {points:,} pts" for did,tier,points in candidates]
        return lines,f"{tracked} members with verified CA data; staff review required."

    @tasks.loop(minutes=30)
    async def audit(self):
        await self.bot.wait_until_ready()
        guild=self.bot.get_guild(GUILD)
        if not guild:
            return
        now=dt.datetime.now(ET)
        if now.hour<8:
            return
        day=now.date().isoformat()
        try:
            with closing(db()) as conn:
                if conn.execute("SELECT 1 FROM runeprofile_audit_days WHERE day=?",(day,)).fetchone():
                    return
            await self.audit_once(guild,day)
        except Exception:
            logger.exception("RuneProfile daily audit failed")

    @audit.before_loop
    async def before_audit(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(60)

async def setup(bot):
    await bot.add_cog(RuneProfileCog(bot))

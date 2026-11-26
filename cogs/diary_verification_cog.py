"""Private, read-only WOM verification handoff for Novus Apex.

Only Discord IDs, current WOM join dates and effective tenure dates are exported.
No usernames, credentials, or raw database records leave Sync.
"""
import asyncio
import datetime as dt
import io
import json
import logging
import os
import sqlite3
from contextlib import closing
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from common import has_novus_member_role, find_primary_wom_membership
from cogs.tasks_cog import (
    get_membership_joined_at, add_calendar_months, has_verifiable_wom_roster,
    NOVUS_TRIAL_WOM_RANKS,
)

GUILD=1532817291048124466
PARENT=1532825723511767246
APEX=1568701512161693757
COUNCIL={1532829195657285843,1532830356875513906}
THREAD="Apex verification • internal"
MARKER="Novus Sync • Apex membership verification v1"
FILENAME="apex-membership.json"
ET=ZoneInfo("America/New_York")
logger=logging.getLogger("WOMBot")
DB=os.path.join(os.getenv("NOVUS_DATA_DIR") or os.path.dirname(os.path.dirname(__file__)),"wom_multi.db")

def connect():
    return sqlite3.connect(DB,timeout=15)

def private_parent(guild):
    ch=guild.get_channel(PARENT)
    if not isinstance(ch,discord.TextChannel) or ch.name.casefold()!="council-chat" or ch.permissions_for(guild.default_role).view_channel:
        return None
    for role in guild.roles:
        if role.id not in COUNCIL and not role.is_bot_managed() and not role.permissions.administrator and ch.permissions_for(role).view_channel:
            return None
    for target,overwrite in ch.overwrites.items():
        if isinstance(target,discord.Member) and not target.bot and overwrite.view_channel is True and not target.guild_permissions.administrator and not any(r.id in COUNCIL for r in target.roles):
            return None
    return ch

class DiaryVerificationCog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.sync_snapshot.start()

    def cog_unload(self):
        self.sync_snapshot.cancel()

    async def get_thread(self,parent):
        with closing(connect()) as conn,conn:
            conn.execute("CREATE TABLE IF NOT EXISTS apex_verification_bridge (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
            saved=conn.execute("SELECT value FROM apex_verification_bridge WHERE key='thread'").fetchone()
        if saved:
            try:
                thread=await self.bot.fetch_channel(int(saved[0]))
            except discord.NotFound:
                return None
            if not isinstance(thread,discord.Thread) or thread.parent_id!=parent.id or not thread.is_private() or thread.owner_id!=self.bot.user.id:
                return None
        else:
            candidates=[t for t in await parent.guild.active_threads() if t.parent_id==parent.id and t.name==THREAD and t.is_private() and t.owner_id==self.bot.user.id]
            if len(candidates)>1:
                return None
            thread=candidates[0] if candidates else None
            if thread is None:
                async for archived in parent.archived_threads(private=True,joined=True,limit=100):
                    if archived.name==THREAD and archived.owner_id==self.bot.user.id:
                        thread=archived
                        break
            if thread is None:
                thread=await parent.create_thread(name=THREAD,type=discord.ChannelType.private_thread,invitable=False,auto_archive_duration=1440,reason="Internal verified WOM data for Novus Apex")
            with closing(connect()) as conn,conn:
                conn.execute("INSERT OR REPLACE INTO apex_verification_bridge VALUES ('thread',?)",(str(thread.id),))
        if thread.archived:
            await thread.edit(archived=False)
        apex=parent.guild.get_member(APEX) or await parent.guild.fetch_member(APEX)
        if not apex.bot or not parent.permissions_for(apex).view_channel:
            return None
        await thread.add_user(apex)
        return thread

    @tasks.loop(minutes=15)
    async def sync_snapshot(self):
        try:
            guild=self.bot.get_guild(GUILD)
            sync=self.bot.get_cog("TasksCog")
            if not guild or not sync:
                return
            parent=private_parent(guild)
            if parent is None:
                logger.warning("Apex verification unavailable: council privacy check failed")
                return
            apex=guild.get_member(APEX) or await guild.fetch_member(APEX)
            if not parent.permissions_for(apex).view_channel:
                logger.warning("Apex verification unavailable: Apex cannot view council parent")
                return
            if not guild.chunked:
                await guild.chunk(cache=True)
            if not guild.chunked:
                return
            with closing(connect()) as conn:
                config=conn.execute("SELECT group_id FROM guild_configs WHERE guild_id=?",(GUILD,)).fetchone()
            if not config or not config[0]:
                return
            group=int(config[0])
            data,status=await sync.get_wom_group(group)
            if status!=200 or not has_verifiable_wom_roster(data) or data.get("id")!=group:
                logger.warning("Apex verification snapshot skipped: WOM unavailable")
                return
            memberships=data["memberships"]
            result={}
            with closing(connect()) as conn:
                cursor=conn.cursor()
                links=cursor.execute("SELECT discord_id,rsn,wom_id FROM links WHERE guild_id=?",(GUILD,)).fetchall()
                for discord_id,rsn,wom_id in links:
                    member=guild.get_member(discord_id)
                    if not member or member.bot or not has_novus_member_role(member) or not wom_id:
                        continue
                    primary=find_primary_wom_membership(rsn,wom_id,memberships)
                    if not primary or primary.get("role","").casefold() in NOVUS_TRIAL_WOM_RANKS:
                        continue
                    joined=get_membership_joined_at(primary)
                    if not joined:
                        continue
                    accounts=sync.get_linked_account_tenures(cursor,GUILD,group,discord_id,memberships)
                    basis=sync.get_highest_discord_tenure_basis(accounts)
                    effective=basis.get("effective_joined_at") if basis else None
                    # The underlying tenure engine handles resets and prior service.
                    # Use the same calendar-date rule as Sync's promotion checks.
                    eligible=add_calendar_months(effective,3).date().isoformat() if effective else None
                    result[str(discord_id)]={"joined":joined.date().isoformat(),"tenure_eligible":eligible,"effective_joined":effective.date().isoformat() if effective else None}
            if not result:
                logger.warning("Apex verification snapshot skipped: no verified members")
                return
            if private_parent(guild)!=parent:
                return
            thread=await self.get_thread(parent)
            if thread is None:
                logger.warning("Apex verification unavailable: private bridge thread not accessible")
                return
            if private_parent(guild)!=parent:
                return
            payload={"version":1,"guild":GUILD,"generated":dt.datetime.now(dt.timezone.utc).isoformat(),"members":result}
            file=discord.File(io.BytesIO(json.dumps(payload,separators=(",",":")).encode()),filename=FILENAME)
            # Discord exempts messages explicitly mentioning the receiving app
            # from message-content redaction; Apex does not need privileged intent.
            content=f"<@{APEX}> {MARKER}"
            mentions=discord.AllowedMentions(everyone=False,roles=False,users=[discord.Object(id=APEX)],replied_user=False)
            async for msg in thread.history(limit=50):
                if msg.author.id==self.bot.user.id and msg.content in (MARKER,content):
                    await msg.edit(content=content,attachments=[file],allowed_mentions=mentions)
                    break
            else:
                await thread.send(content,file=file,silent=True,allowed_mentions=mentions)
            logger.info("Apex verification snapshot published thread=%s members=%s",thread.id,len(result))
        except Exception:
            logger.exception("Apex verification snapshot failed")

    @sync_snapshot.before_loop
    async def before_sync(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(25)

async def setup(bot):
    await bot.add_cog(DiaryVerificationCog(bot))

"""One-time Council announcement of the PvM Diary development plan."""
import asyncio
import logging
import sqlite3
from contextlib import closing
import discord
from discord.ext import commands

logger = logging.getLogger("WOMBot")
GUILD = 1532817291048124466
CHANNEL = 1532825723511767246
COUNCIL = {1532829195657285843,1532830356875513906}
KEY = "diary_council_notice_20261125"
BODY = """## Novus Apex | PvM Diary Integration

We're building a live PvM Diary directly into Apex. Every Member's approved PBs will automatically count toward their diary (including all teammates), with original proof carried over. Members will use `/diary` to view their private progress; no spreadsheet or dedicated channel needed.

**Proposed progression**
• Challenger: 1 ToB, 1 CM, ToA 300 Solo, Inferno OR Colosseum
• Vanguard: 2 ToB, 2 CM, ToA 300 Solo, Inferno AND Colosseum
• Paragon: all 10 challenges
Each time must meet its rank threshold. Runs must be on/after the member's WOM join date, all teammates must be clan members at the time, and specialty ranks require 3 months of tenure.

**Review:** Request Review links to our existing MEE6 PvM Diary panel. On ticket creation, Apex will post the applicant's diary and original proof for staff to review. Staff approve and assign the rank; achievements feed into the Monthly Novus Report.

Development has started; the existing sheet/ticket workflow stays in place until verification and testing are complete."""
class DiaryCouncilNotice(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.sent=False
    @commands.Cog.listener()
    async def on_ready(self):
        if self.sent:
            return
        self.sent=True
        await asyncio.sleep(20)
        try:
            with closing(sqlite3.connect("wom_multi.db",timeout=15)) as db:
                if db.execute("SELECT 1 FROM bot_stats WHERE key=?",(KEY,)).fetchone():
                    return
            guild=self.bot.get_guild(GUILD)
            channel=guild.get_channel(CHANNEL) if guild else None
            if not isinstance(channel,discord.TextChannel) or channel.name.casefold()!="council-chat" or channel.permissions_for(guild.default_role).view_channel:
                logger.warning("Diary council notice skipped: invalid channel")
                return
            for role in guild.roles:
                if role.id not in COUNCIL and not role.is_bot_managed() and not role.permissions.administrator and channel.permissions_for(role).view_channel:
                    logger.warning("Diary council notice skipped: privacy check")
                    return
            for target,overwrite in channel.overwrites.items():
                if isinstance(target,discord.Member) and not target.bot and overwrite.view_channel is True and not any(r.id in COUNCIL for r in target.roles) and not target.guild_permissions.administrator:
                    logger.warning("Diary council notice skipped: privacy check")
                    return
            async for msg in channel.history(limit=500):
                if msg.author.id==self.bot.user.id and msg.content==BODY:
                    message=msg
                    break
            else:
                message=await channel.send(BODY,allowed_mentions=discord.AllowedMentions.none())
            logger.info("Diary council notice posted channel=%s message=%s",channel.id,message.id)
            with closing(sqlite3.connect("wom_multi.db",timeout=15)) as db,db:
                db.execute("INSERT OR IGNORE INTO bot_stats(key,value) VALUES (?,?)",(KEY,str(message.id)))
        except Exception:
            logger.exception("Diary council notice failed")
async def setup(bot):
    await bot.add_cog(DiaryCouncilNotice(bot))

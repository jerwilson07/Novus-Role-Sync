"""One-time cosmetic migration of existing Novus membership messages."""
import asyncio
import datetime
import logging
import re
from contextlib import closing

import discord
from discord.ext import commands

from cogs.application_cog import GUILD, CATEGORY, REPORT_TITLE, db, get_report
from cogs.vouches_cog import VouchesCog, NOVUS_MOD_CHAT_CHANNEL_ID

logger = logging.getLogger("WOMBot")
VERSION = "membership-style-v2-20261125"

def brand(embed, logo, footer=None):
    embed.set_thumbnail(url=logo)
    if footer is not None:
        embed.set_footer(text=footer)
    return embed

def migrate_details(message, logo):
    old = message.embeds[0]
    if old.title == "Novus Application Details":
        revised = old.copy()
        brand(revised, logo)
        return [revised] + [e.copy() for e in message.embeds[1:]]
    if old.title != "Application Details" or not message.attachments:
        return None
    # Parse only the exact original field layout. Never guess missing content.
    match = re.fullmatch(r"\*\*Applicant:\*\* ([^\n]+)\n\*\*Account type:\*\* ([^\n]+)\n\*\*Membership option:\*\* ([^\n]+)\n\*\*How you found us:\*\* (.+)", old.description or "", re.S)
    if not match:
        return None
    applicant, account, pathway, referral = match.groups()
    if len(referral) > 1024 or any(len(x)>1024 for x in (account,pathway)):
        return None
    revised = discord.Embed(title="Novus Application Details", description=f"**Applicant:** {applicant}", color=discord.Color.blurple())
    brand(revised, logo, "Novus Membership • Staff review pending")
    for name, value in (("Account Type",account),("Membership Pathway",pathway),("How They Found Us",referral)):
        revised.add_field(name=name,value=value,inline=False)
    for field in old.fields:
        revised.add_field(name=field.name,value=field.value,inline=field.inline)
    count = len(message.attachments)
    revised.add_field(name="Gear Screenshots",value=f"{count} screenshot{'s' if count != 1 else ''} attached",inline=False)
    revised.add_field(name="Application Status",value="✅ **Application submitted**\nGear verification pending\nStaff approval pending",inline=False)
    revised.set_image(url=f"attachment://{message.attachments[0].filename}")
    embeds=[revised]
    for i, attachment in enumerate(message.attachments[1:],2):
        extra=discord.Embed(title=f"Gear Screenshot {i} of {count}",color=discord.Color.blurple())
        extra.set_image(url=f"attachment://{attachment.filename}")
        embeds.append(extra)
    return embeds

def migrate_staff(message, logo):
    content=message.content or ""
    if len(message.embeds)==1 and message.embeds[0].title in ("Membership Reviewed","✅ Membership Approved"):
        old=message.embeds[0]
        prefix="Novus Membership • "
        footer=old.footer.text or ""
        if not footer.startswith(prefix) or any(f.name=="Status" for f in old.fields):
            return None
        status=footer[len(prefix):]
        if status not in ("Existing Member • No membership promotion required","Trial Member → Member","Member access granted"):
            return None
        embed=old.copy()
        for i,f in enumerate(old.fields):
            if f.name in ("Member","Reviewed by","Approved by","Pathway"):
                embed.set_field_at(i,name=f.name,value=f.value.rstrip("\n\u200b")+"\n\u200b",inline=False)
        embed.add_field(name="Status",value="Existing member • No promotion required" if status.startswith("Existing Member") else "✅ "+status,inline=False)
        brand(embed,logo,"Novus Membership")
        return content,embed
    if not message.embeds:
        # Only transform the bot's exact old approval format. Historical
        # pathways cannot be inferred from a generic /approve log.
        match=re.fullmatch(r"\*\*(Reviewed existing membership|Approved new member):\*\* (<@!?\d+>) \((.*?)\) by (<@!?\d+>)\.",content,re.S)
        if match:
            action, member, _name, actor=match.groups()
            reviewed=action=="Reviewed existing membership"
            embed=discord.Embed(title="Membership Reviewed" if reviewed else "✅ Membership Approved",color=discord.Color.blurple() if reviewed else discord.Color.green(),timestamp=message.created_at)
            embed.add_field(name="Member",value=member+"\n\u200b",inline=False)
            if not reviewed:
                embed.add_field(name="Pathway",value="Not recorded in original log\n\u200b",inline=False)
            embed.add_field(name="Reviewed by" if reviewed else "Approved by",value=actor+"\n\u200b",inline=False)
            embed.add_field(name="Status",value="Existing member • No promotion required" if reviewed else "✅ Member access granted",inline=False)
            brand(embed,logo,"Novus Membership")
            return "",embed
        match=re.fullmatch(r"\*\*New Trial Vouch\*\*\n(<@!?\d+>) vouched for (<@!?\d+>) \(\*\*(\d+)/(\d+) vouches\*\*\)\.",content)
        if match:
            voucher,trial,total,required=match.groups()
            embed=discord.Embed(title="New Trial Vouch",color=discord.Color.green() if int(total)>=int(required) else discord.Color.blurple(),timestamp=message.created_at)
            embed.add_field(name="Trial Member",value=trial,inline=False)
            embed.add_field(name="Vouched by",value=voucher,inline=False)
            embed.add_field(name="Vouch Progress",value=f"**{total}/{required}** unique vouches",inline=False)
            brand(embed,logo,"Novus Membership • Raid Trial")
            return "",embed
    elif len(message.embeds)==1 and message.embeds[0].title=="Trial Member Ready for Staff Review":
        old=message.embeds[0]
        if old.footer.text=="Novus Membership • Raid Trial":
            return None
        embed=old.copy()
        match=re.match(r"(<@!?\d+>) has received \*\*(\d+) unique vouches\*\*",old.description or "")
        if match:
            member,count=match.groups()
            embed.description=f"**Trial Member:** {member}"
            embed.insert_field_at(0,name="Vouch Progress",value=f"✅ **{count}/3** unique vouches",inline=False)
            embed.insert_field_at(1,name="Application Status",value="Vouch requirement met\nStaff approval pending",inline=False)
        brand(embed,logo,"Novus Membership • Raid Trial")
        return content,embed
    return None

class MembershipStyleMigrationCog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.running=False

    @commands.Cog.listener()
    async def on_ready(self):
        if self.running:
            return
        self.running=True
        try:
            await asyncio.sleep(15)
            with closing(db()) as conn, conn:
                conn.execute("CREATE TABLE IF NOT EXISTS membership_style_migrations (version TEXT PRIMARY KEY, finished_at TEXT NOT NULL)")
                if conn.execute("SELECT 1 FROM membership_style_migrations WHERE version=?",(VERSION,)).fetchone():
                    return
                reports=conn.execute("SELECT channel_id, message_id FROM application_kc_reports").fetchall()
                forms=conn.execute("SELECT channel_id, response_id FROM application_form_submissions").fetchall()
            guild=self.bot.get_guild(GUILD)
            if not guild:
                return
            application=self.bot.get_cog("ApplicationCog")
            if application is None:
                return
            logo=(guild.icon or self.bot.user.display_avatar).url
            counts={"reports":0,"submissions":0,"staff":0,"missing":0,"skipped":0,"errors":0}
            for kind, rows in (("reports",reports),("submissions",forms)):
                for channel_id,message_id in rows:
                    try:
                        channel=guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                        if not isinstance(channel,discord.TextChannel) or channel.guild.id!=GUILD:
                            counts["skipped"]+=1
                            continue
                        # Stored message IDs are authoritative, including archived tickets.
                        async with application.locks.setdefault(channel_id,asyncio.Lock()):
                            message=await channel.fetch_message(message_id)
                            if message.author.id!=self.bot.user.id or not message.embeds:
                                counts["skipped"]+=1
                                continue
                            if kind=="reports":
                                if message.embeds[0].title!=REPORT_TITLE:
                                    counts["skipped"]+=1
                                    continue
                                embed=message.embeds[0].copy()
                                footer=embed.footer.text or ""
                                if footer.startswith("Wise Old Man • Last updated: "):
                                    footer=footer.replace("Wise Old Man • Last updated: ","Novus Membership • WOM: ",1)
                                brand(embed,logo,footer or None)
                                report=get_report(channel_id)
                                desc=embed.description or ""
                                if report and report[2]==message_id and report[0] and desc.startswith("**Applicant:** ") and not desc.startswith(f"**Applicant:** <@{report[0]}>"):
                                    first,sep,rest=desc.partition("\n")
                                    if not first.startswith("**Applicant:** <@"):
                                        embed.description=f"**Applicant:** <@{report[0]}>\n**RSN:** "+first.removeprefix("**Applicant:** ")+sep+rest
                                for i,f in enumerate(embed.fields):
                                    if f.name=="Application Status":
                                        val="\n".join("▫️ "+line if line in ("Gear verification pending","Staff approval pending") else line for line in f.value.split("\n"))
                                        embed.set_field_at(i,name=f.name,value=val,inline=f.inline)
                                        if "KC thresholds not met" in val and "approval complete" not in val and any(x.name=="Alternative Pathways" for x in embed.fields):
                                            embed.color=discord.Color.gold()
                                    elif f.name=="Alternative Pathways":
                                        embed.set_field_at(i,name=f.name,value=f.value.rstrip("\n\u200b")+"\n\u200b",inline=f.inline)
                                if embed.to_dict()==message.embeds[0].to_dict():
                                    continue
                                await message.edit(embeds=[embed]+[e.copy() for e in message.embeds[1:]],allowed_mentions=discord.AllowedMentions.none())
                            else:
                                embeds=migrate_details(message,logo)
                                if embeds is None:
                                    counts["skipped"]+=1
                                    continue
                                if [e.to_dict() for e in embeds]==[e.to_dict() for e in message.embeds]:
                                    continue
                                # Explicitly retain the originals; no download/reupload.
                                await message.edit(embeds=embeds,attachments=message.attachments,allowed_mentions=discord.AllowedMentions.none())
                            counts[kind]+=1
                        await asyncio.sleep(0.25)
                    except discord.NotFound:
                        counts["missing"]+=1
                    except Exception:
                        counts["errors"]+=1
                        logger.exception("Membership style migration failed channel=%s message=%s",channel_id,message_id)
            channels={}
            for channel in (guild.get_channel(NOVUS_MOD_CHAT_CHANNEL_ID),VouchesCog.staff_channel(guild)):
                if isinstance(channel,discord.TextChannel) and channel.guild.id==GUILD:
                    channels[channel.id]=channel
            for channel in channels.values():
                try:
                    async for message in channel.history(limit=1000):
                        if message.author.id!=self.bot.user.id:
                            continue
                        result=migrate_staff(message,logo)
                        if result is None:
                            continue
                        content,embed=result
                        await message.edit(content=content,embed=embed,allowed_mentions=discord.AllowedMentions.none())
                        counts["staff"]+=1
                        await asyncio.sleep(0.25)
                except Exception:
                    counts["errors"]+=1
                    logger.exception("Membership staff style migration failed channel=%s",channel.id)
            logger.info("Membership style migration totals %s",counts)
            if not counts["errors"]:
                with closing(db()) as conn, conn:
                    conn.execute("INSERT OR IGNORE INTO membership_style_migrations VALUES (?,?)",(VERSION,datetime.datetime.now(datetime.timezone.utc).isoformat()))
        except Exception:
            logger.exception("Membership style migration failed")
        finally:
            self.running=False

async def setup(bot):
    await bot.add_cog(MembershipStyleMigrationCog(bot))

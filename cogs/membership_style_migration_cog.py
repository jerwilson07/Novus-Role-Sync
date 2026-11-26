"""One-time cosmetic migration of existing Novus membership messages."""
import asyncio
import datetime
import logging
import re
from contextlib import closing

import discord
from novus_logging import check, notice, bind_novus_ui, replace_status_marks
from discord.ext import commands

from cogs.application_cog import GUILD, CATEGORY, REPORT_TITLE, REPORT_TITLES, DETAILS_TITLE, DETAILS_TITLES, db, get_report
from cogs.vouches_cog import VouchesCog, NOVUS_MOD_CHAT_CHANNEL_ID

logger = logging.getLogger("WOMBot")
VERSION = "membership-style-v5-gear-attachments-20261125"

def brand(embed, logo, footer=None):
    embed.set_thumbnail(url=logo)
    if footer is not None:
        embed.set_footer(text=footer)
    return embed

def migrate_details(message, logo):
    old = message.embeds[0]
    if old.title in DETAILS_TITLES:
        revised = old.copy()
        revised.title = DETAILS_TITLE
        brand(revised, logo)
        # Rebind images to retained attachment names, not expired CDN URLs.
        images = [a for a in message.attachments if (a.content_type or "").startswith("image/") or re.search(r"\.(png|jpe?g|gif|webp|bmp|avif)$", a.filename, re.I)]
        if not images:
            # Never silently claim missing evidence is available.
            for i, field in enumerate(revised.fields):
                if field.name == "Gear Screenshots":
                    revised.set_field_at(i, name=field.name, value="No accessible screenshot attachments. Please re-upload gear screenshots for staff review.", inline=False)
            revised.set_image(url=None)
            return [revised]
        count = len(images)
        for i, field in enumerate(revised.fields):
            if field.name == "Gear Screenshots":
                revised.set_field_at(i, name=field.name, value=f"{count} screenshot{'s' if count != 1 else ''} attached", inline=False)
        revised.set_image(url=f"attachment://{images[0].filename}")
        embeds = [revised]
        for i, attachment in enumerate(images[1:], 2):
            extra = discord.Embed(title=f"Gear Screenshot {i} of {count}", color=discord.Color.blurple())
            extra.set_image(url=f"attachment://{attachment.filename}")
            embeds.append(extra)
        return embeds
    if old.title != "Application Details" or not message.attachments:
        return None
    # Parse only the exact original field layout. Never guess missing content.
    match = re.fullmatch(r"\*\*Applicant:\*\* ([^\n]+)\n\*\*Account type:\*\* ([^\n]+)\n\*\*Membership option:\*\* ([^\n]+)\n\*\*How you found us:\*\* (.+)", old.description or "", re.S)
    if not match:
        return None
    applicant, account, pathway, referral = match.groups()
    if len(referral) > 1024 or any(len(x)>1024 for x in (account,pathway)):
        return None
    revised = discord.Embed(title=DETAILS_TITLE, description=f"**Applicant:** {applicant}", color=discord.Color.blurple())
    brand(revised, logo, "Novus Membership • Staff review pending")
    for name, value in (("Account Type",account),("Membership Pathway",pathway),("How They Found Us",referral)):
        revised.add_field(name=name,value=value,inline=False)
    for field in old.fields:
        revised.add_field(name=field.name,value=field.value,inline=field.inline)
    count = len(message.attachments)
    revised.add_field(name="Gear Screenshots",value=f"{count} screenshot{'s' if count != 1 else ''} attached",inline=False)
    revised.add_field(name="Application Status",value=f"{check()} **Application submitted**\n▫️ Gear verification pending\n▫️ Staff approval pending",inline=False)
    revised.set_image(url=f"attachment://{message.attachments[0].filename}")
    embeds=[revised]
    for i, attachment in enumerate(message.attachments[1:],2):
        extra=discord.Embed(title=f"Gear Screenshot {i} of {count}",color=discord.Color.blurple())
        extra.set_image(url=f"attachment://{attachment.filename}")
        embeds.append(extra)
    return embeds

def brand_checks(embed):
    revised=embed.to_dict()
    def convert(value):
        if isinstance(value,str):
            return replace_status_marks(value)
        if isinstance(value,list):
            return [convert(v) for v in value]
        if isinstance(value,dict):
            return {k:convert(v) for k,v in value.items()}
        return value
    return discord.Embed.from_dict(convert(revised))

def migrate_staff(message, logo):
    content=message.content or ""
    if len(message.embeds)==1 and message.embeds[0].title in ("Membership Reviewed","✅ Membership Approved",f"{check()} Membership Approved"):
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
        embed.add_field(name="Status",value="Existing member • No promotion required" if status.startswith("Existing Member") else f"{check()} "+status,inline=False)
        brand(embed,logo,"Novus Membership")
        return content,embed
    if not message.embeds:
        # Only transform the bot's exact old approval format. Historical
        # pathways cannot be inferred from a generic /approve log.
        match=re.fullmatch(r"\*\*(Reviewed existing membership|Approved new member):\*\* (<@!?\d+>) \((.*?)\) by (<@!?\d+>)\.",content,re.S)
        if match:
            action, member, _name, actor=match.groups()
            reviewed=action=="Reviewed existing membership"
            embed=discord.Embed(title="Membership Reviewed" if reviewed else f"{check()} Membership Approved",color=discord.Color.blurple() if reviewed else discord.Color.green(),timestamp=message.created_at)
            embed.add_field(name="Member",value=member+"\n\u200b",inline=False)
            if not reviewed:
                embed.add_field(name="Pathway",value="Not recorded in original log\n\u200b",inline=False)
            embed.add_field(name="Reviewed by" if reviewed else "Approved by",value=actor+"\n\u200b",inline=False)
            embed.add_field(name="Status",value="Existing member • No promotion required" if reviewed else f"{check()} Member access granted",inline=False)
            brand(embed,logo,"Novus Membership")
            return "",embed
        match=re.fullmatch(r"\*\*New Trial Vouch\*\*\n(<@!?\d+>) vouched for (<@!?\d+>) \(\*\*(\d+)/(\d+) vouches\*\*\)\.",content)
        if match:
            voucher,trial,total,required=match.groups()
            embed=discord.Embed(title="New Trial Vouch",color=discord.Color.green() if int(total)>=int(required) else discord.Color.blurple(),timestamp=message.created_at)
            embed.add_field(name="Trial Member",value=trial,inline=False)
            embed.add_field(name="Vouched by",value=voucher,inline=False)
            embed.add_field(name="Vouch Progress",value=f"{check() if int(total)>=int(required) else '▫️'} **{total}/{required}** unique vouches",inline=False)
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
            embed.insert_field_at(0,name="Vouch Progress",value=f"{check()} **{count}/3** unique vouches",inline=False)
            embed.insert_field_at(1,name="Application Status",value=f"{check()} Vouch requirement met\n▫️ Staff approval pending",inline=False)
        brand(embed,logo,"Novus Membership • Raid Trial")
        return content,embed
    return None

class MembershipStyleMigrationCog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.running=False

    async def refresh_branded_titles(self, guild, application, reports, forms):
        key="membership-title-star-v1"
        with closing(db()) as conn:
            if conn.execute("SELECT 1 FROM membership_style_migrations WHERE version=?", (key,)).fetchone():
                return
        changed=errors=skipped=0
        for rows, old_title, new_title in ((reports, "Novus Applicant Eligibility", REPORT_TITLE),
                                           (forms, "Novus Application Details", DETAILS_TITLE)):
            for channel_id,message_id in rows:
                try:
                    channel=guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                    if not isinstance(channel,discord.TextChannel) or channel.guild.id!=GUILD:
                        continue
                    async with application.locks.setdefault(channel_id,asyncio.Lock()):
                        msg=await channel.fetch_message(message_id)
                        if msg.author.id!=self.bot.user.id or not msg.embeds or msg.embeds[0].title!=old_title:
                            continue
                        # Never edit evidence cards when Discord has not returned their attachments.
                        if new_title==DETAILS_TITLE and any(e.image.url for e in msg.embeds) and not msg.attachments:
                            skipped+=1
                            continue
                        embeds=[e.copy() for e in msg.embeds]
                        embeds[0].title=new_title
                        await msg.edit(embeds=embeds, attachments=msg.attachments, allowed_mentions=discord.AllowedMentions.none())
                        changed+=1
                except discord.NotFound:
                    continue
                except Exception:
                    errors+=1
                    logger.exception("Membership title refresh failed channel=%s message=%s",channel_id,message_id)
        logger.info("Membership title refresh changed=%s skipped_evidence=%s errors=%s",changed,skipped,errors)
        if not errors:
            with closing(db()) as conn, conn:
                conn.execute("INSERT OR IGNORE INTO membership_style_migrations VALUES (?,?)",(key,datetime.datetime.now(datetime.timezone.utc).isoformat()))

    @commands.Cog.listener()
    async def on_ready(self):
        if self.running:
            return
        self.running=True
        try:
            await asyncio.sleep(15)
            with closing(db()) as conn, conn:
                conn.execute("CREATE TABLE IF NOT EXISTS membership_style_migrations (version TEXT PRIMARY KEY, finished_at TEXT NOT NULL)")
                style_done=bool(conn.execute("SELECT 1 FROM membership_style_migrations WHERE version=?",(VERSION,)).fetchone())
                reports=conn.execute("SELECT channel_id, message_id FROM application_kc_reports").fetchall()
                forms=conn.execute("SELECT channel_id, response_id FROM application_form_submissions").fetchall()
            guild=self.bot.get_guild(GUILD)
            if not guild:
                return
            application=self.bot.get_cog("ApplicationCog")
            if application is None:
                return
            if style_done:
                await self.refresh_branded_titles(guild, application, reports, forms)
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
                                if message.embeds[0].title not in REPORT_TITLES:
                                    counts["skipped"]+=1
                                    continue
                                embed=message.embeds[0].copy()
                                embed.title=REPORT_TITLE
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
                                embed=brand_checks(embed)
                                if embed.to_dict()==message.embeds[0].to_dict():
                                    continue
                                await message.edit(embeds=[embed]+[e.copy() for e in message.embeds[1:]],allowed_mentions=discord.AllowedMentions.none())
                            else:
                                embeds=migrate_details(message,logo)
                                if embeds is None:
                                    counts["skipped"]+=1
                                    continue
                                embeds=[brand_checks(e) for e in embeds]
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
                            if len(message.embeds)!=1 or message.embeds[0].title not in ("Membership Reviewed","✅ Membership Approved",f"{check()} Membership Approved","New Trial Vouch","Trial Member Ready for Staff Review"):
                                continue
                            revised=brand_checks(message.embeds[0])
                            if revised.to_dict()==message.embeds[0].to_dict():
                                continue
                            result=(message.content or "",revised)
                        content,embed=result
                        content=replace_status_marks(content)
                        embed=brand_checks(embed)
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

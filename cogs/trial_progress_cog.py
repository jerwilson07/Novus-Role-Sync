"""Twice-weekly consolidated trial progress with real Discord mentions."""
import asyncio
import datetime as dt
import json
import logging
import sqlite3
from contextlib import closing
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands,tasks
from cogs.application_cog import GUILD,EXTRAS,RAIDS
from cogs.trial_kc_eligibility_cog import episode,kc_qualification,HUB
from cogs.vouches_cog import is_trial,get_vouches
from cogs.tasks_cog import COUNCIL_ROLE_ID,MODERATOR_ROLE_ID,TRIAL_MOD_ROLE_ID
from novus_logging import check

ET=ZoneInfo("America/New_York")
logger=logging.getLogger("WOMBot")
LINK="https://discord.com/channels/1532817291048124466/1537889319220551753/1565523333762785311"
TITLE="✦ Novus Trial Member Progress"

def db():
    return sqlite3.connect("wom_multi.db",timeout=5)

def init():
    with closing(db()) as c,c:
        c.execute("CREATE TABLE IF NOT EXISTS trial_progress_deliveries (day TEXT PRIMARY KEY,message_id INTEGER NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS trial_progress_ca (episode_id INTEGER PRIMARY KEY,completed TEXT NOT NULL)")

def due(now):
    local=now.astimezone(ET)
    # First report requested immediately on Nov 26, replacing today's 5 PM post.
    return local.date()==dt.date(2026,11,26) or (local.weekday() in (0,3) and local.hour>=17)

def bar(n,d,slots=10):
    filled=max(0,min(slots,n*slots//d))
    return "🟩"*filled+"⬛"*(slots-filled)

def deadline_text(deadline,now):
    if not deadline:
        return "▫️ Trial start/deadline unverified"
    days=(deadline.astimezone(ET).date()-now.astimezone(ET).date()).days
    stamp=f"<t:{int(deadline.timestamp())}:D>"
    if deadline<=now:
        return f"✖️ Deadline passed • {stamp}"
    if days<=3:
        return f"⚠️ {max(0,days)} days left • {stamp}"
    return f"{days} days left • {stamp}"

def fresh(payload,now):
    try:
        stamp=dt.datetime.fromisoformat(payload["snapshot"].replace("Z","+00:00"))
        return dt.timedelta(0)<=now-stamp<=dt.timedelta(days=7)
    except (KeyError,TypeError,ValueError,AttributeError):
        return False

class TrialProgressCog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        init()
        self.run.start()

    def cog_unload(self):
        self.run.cancel()

    async def entry(self,tc,guild,member,now):
        pathway,start,deadline=await tc.trial_timing(guild,member)
        row=episode(member.id)
        eid=row["episode_id"] if row else None
        end=deadline_text(deadline,now)
        current_ca=None
        if pathway=="raidtrial":
            if not start:
                detail="▫️ Vouch period unverified"
            else:
                vouchers=set()
                for rec in get_vouches(GUILD,member.id):
                    try:
                        stamp=dt.datetime.fromisoformat(rec[3].replace("Z","+00:00"))
                        if start<=stamp and (not deadline or stamp<=deadline):
                            vouchers.add(rec[0])
                    except (TypeError,ValueError):
                        continue
                n=len(vouchers)
                detail=f"{bar(n,3,3)} **{n}/3 vouches** • {max(0,3-n)} remaining"
                if n>=3:
                    detail+=f" {check()}"
        elif pathway in ("kc","rpp"):
            payload=await tc.wom_payload(member)
            if not payload or not fresh(payload,now):
                detail="⚠️ WOM profile update required"
            else:
                vals=payload["values"]
                total,qual=kc_qualification(vals)
                cape=any(type(vals.get(key)) is int and vals[key]>0 for _,key in EXTRAS)
                target=100 if cape else 200
                known=any(type(vals.get(key)) is int for _,key in RAIDS)
                detail=f"{bar(total,target)} **{total}/{target} raid KC** • {max(0,target-total)} remaining" if known else "▫️ Raid KC unverified"
                if cape:
                    detail+=f"\n{check()} Inferno / Colosseum requirement met"
                if qual:
                    detail+=f"\n{check()} KC eligibility met"
            if pathway=="rpp":
                progress=await tc.rpp_member_progress(payload)
                complete={t["id"] for t in progress["ca"] if t["status"]=="complete"}
                verified=any(t["status"] in ("complete","incomplete") for t in progress["ca"])
                current_ca=complete if verified else None
                detail=f"{bar(len(complete),progress['ca_total'])} **{len(complete)}/{progress['ca_total']} required CAs**" if verified else "▫️ RuneProfile CA data unverified"
                if verified:
                    with closing(db()) as c:
                        prev=c.execute("SELECT completed FROM trial_progress_ca WHERE episode_id=?",(eid,)).fetchone()
                    if prev:
                        new=complete-set(json.loads(prev[0]))
                        if new:
                            detail+="\n"+check()+" New: "+", ".join(t["name"] for t in progress["ca"] if t["id"] in new)
                for boss in progress["kc"]:
                    ca=[t for t in progress["ca"] if t["boss"].casefold().removeprefix("the ")==boss["name"].casefold() or (boss["name"]=="Corrupted Gauntlet" and t["boss"]=="Corrupted Hunllef")]
                    kc=boss["kc"] if payload and fresh(payload,now) else None
                    limit=str(boss["minimum"]) if boss["minimum"]==boss["upper"] else f"{boss['minimum']}-{boss['upper']}"
                    line=f"▫️ {boss['name']}: {kc if kc is not None else '?'}/{limit} KC"
                    if ca:
                        line+=f" • {sum(t['status']=='complete' for t in ca)}/{len(ca)} CAs"
                    detail+="\n"+line
                detail+="\nMentor / recorded proof still required."
        else:
            detail="⚠️ Pathway pending staff confirmation"
        return pathway or "unknown",f"**{member.mention}**\n{detail}\n{end}",eid,current_ca

    async def revise_sirgrotesque(self,guild,tc,hub,now):
        """One-time requested edit of the existing Nov 26 trial report."""
        if now.astimezone(ET).date()!=dt.date(2026,11,26):
            return
        sync=self.bot.get_cog("TasksCog")
        if not sync:
            return
        key=f"novus_sirgrotesque_trial_revision_20261126:{GUILD}"
        with closing(db()) as conn:
            if sync._one_time_delivery_sent(conn.cursor(),key):
                return
        matches=[m for m in guild.members if m.display_name.casefold()=="sirgrotesque" and is_trial(m)]
        if len(matches)!=1:
            return
        member=await guild.fetch_member(matches[0].id)
        if not is_trial(member):
            return
        group,status=await sync.get_wom_group(25802)
        if status!=200 or group.get("id")!=25802:
            return
        roster=[m for m in group.get("memberships",[]) if any(isinstance(n,str) and n.strip().casefold()=="sirgrotesque" for n in (m.get("player",{}).get("username"),m.get("player",{}).get("displayName")))]
        if len(roster)!=1:
            logger.info("SirGrotesque trial revision: WOM clan roster not yet updated")
            return
        payload=await tc.wom_payload(member)
        if not payload or not fresh(payload,now):
            logger.info("SirGrotesque trial revision: WOM KC snapshot not yet fresh")
            return
        vals=payload["values"]
        if not any(type(vals.get(key)) is int for _,key in RAIDS):
            logger.info("SirGrotesque trial revision: raid KC still unverified")
            return
        pathway,line,eid,_=await self.entry(tc,guild,member,now)
        if pathway!="kc" or "profile update required" in line or "KC unverified" in line:
            return
        msg=await hub.fetch_message(1575570534094213215)
        if msg.author.id!=self.bot.user.id or TITLE not in msg.content:
            return
        import re
        pattern=r"(?m)^\*\*<@!?"+str(member.id)+r">\*\*\n[^\n]*(?:\n[^\n]+)*?(?=\n\n|\Z)"
        content,count=re.subn(pattern,lambda _:line,msg.content)
        if count!=1 or len(content)>2000:
            logger.warning("SirGrotesque trial revision could not identify unique message entry")
            return
        await msg.edit(content=content,allowed_mentions=discord.AllowedMentions.none())
        sync._mark_one_time_delivery(key,msg.id)
        logger.info("SirGrotesque trial progress revised message=%s kc=%s snapshot=%s",msg.id,kc_qualification(vals)[0],payload.get("snapshot"))

    @tasks.loop(minutes=10)
    async def run(self):
        now=dt.datetime.now(dt.timezone.utc)
        if not due(now):
            return
        day=now.astimezone(ET).date().isoformat()
        try:
            with closing(db()) as c:
                delivered=c.execute("SELECT 1 FROM trial_progress_deliveries WHERE day=?",(day,)).fetchone()
            guild=self.bot.get_guild(GUILD)
            tc=self.bot.get_cog("TrialKCEligibilityCog")
            if not guild or not tc or not guild.me:
                return
            hub=guild.get_channel(HUB)
            if not isinstance(hub,discord.TextChannel) or hub.name!="trial-member-hub":
                return
            perms=hub.permissions_for(guild.me)
            if not (perms.send_messages and perms.read_message_history):
                return
            if delivered:
                if not guild.chunked:
                    await guild.chunk(cache=True)
                if guild.chunked:
                    await self.revise_sirgrotesque(guild,tc,hub,now)
                return
            roles=[guild.get_role(r) for r in (COUNCIL_ROLE_ID,MODERATOR_ROLE_ID,TRIAL_MOD_ROLE_ID)]
            if any(r is None for r in roles):
                logger.warning("Trial progress staff role mapping unavailable")
                return
            if any(not r.mentionable for r in roles) and not perms.mention_everyone:
                logger.warning("Trial progress staff ping permission unavailable")
                return
            if not guild.chunked:
                await guild.chunk(cache=True)
            if not guild.chunked:
                return
            trial_roles=[r for r in guild.roles if r.name.strip().casefold()=="trial member"]
            if len(trial_roles)!=1:
                return
            members=[m for m in trial_roles[0].members if not m.bot and is_trial(m)]
            if not members:
                return
            marker=f"Novus Membership • WOM + RuneProfile • {day}"
            # Recover a successfully posted report after a restart.
            async for msg in hub.history(limit=100):
                if msg.author.id==self.bot.user.id and marker in msg.content and TITLE in msg.content:
                    with closing(db()) as c,c:
                        c.execute("INSERT OR IGNORE INTO trial_progress_deliveries VALUES (?,?)",(day,msg.id))
                    return
            sections={k:[] for k in ("kc","rpp","raidtrial","unknown")}
            snapshots=[]
            verified_members=[]
            for member in members:
                try:
                    live=await guild.fetch_member(member.id)
                    if not is_trial(live):
                        continue
                    pathway,line,eid,completed=await self.entry(tc,guild,live,now)
                    sections.get(pathway,sections["unknown"]).append(line)
                    verified_members.append(live)
                    if completed is not None and eid:
                        snapshots.append((eid,json.dumps(sorted(completed))))
                except Exception:
                    logger.exception("Trial progress member lookup failed discord=%s",member.id)
                    sections["unknown"].append(f"**{member.mention}**\n⚠️ Progress temporarily unavailable")
                    verified_members.append(member)
            if not verified_members:
                return
            headings={"kc":"KC Requirements","rpp":"Raids Prep Program","raidtrial":"Raid Trial","unknown":"Pending Classification"}
            content=f"## {TITLE}\n"+" ".join(r.mention for r in roles)+f"\n-# {len(verified_members)} active Trial Members\n"
            for key,entries in sections.items():
                if entries:
                    content+="\n**"+headings[key]+"**\n"+"\n\n".join(entries)+"\n"
            content+=f"\n[Apply for Membership]({LINK})\n-# {marker} • Mondays & Thursdays, 5 PM ET"
            # Don't truncate member mentions or progress. Rare larger reports
            # use a single message with text attachment rather than losing rows.
            if len(content)>2000:
                import io
                mentions=" ".join(m.mention for m in verified_members)
                header=f"## {TITLE}\n"+" ".join(r.mention for r in roles)+"\n"+mentions+f"\n-# {marker} • Full progress attached"
                if len(header)>2000:
                    logger.warning("Trial progress report exceeds safe Discord mention limit")
                    return
                file=discord.File(io.BytesIO(content.encode()),filename="trial-progress.txt")
                msg=await hub.send(header,file=file,allowed_mentions=discord.AllowedMentions(users=verified_members,roles=roles,everyone=False,replied_user=False))
            else:
                msg=await hub.send(content,allowed_mentions=discord.AllowedMentions(users=verified_members,roles=roles,everyone=False,replied_user=False))
            with closing(db()) as c,c:
                c.execute("INSERT OR IGNORE INTO trial_progress_deliveries VALUES (?,?)",(day,msg.id))
                c.executemany("INSERT OR REPLACE INTO trial_progress_ca VALUES (?,?)",snapshots)
            logger.info("Novus trial progress posted day=%s message=%s members=%s",day,msg.id,len(verified_members))
        except Exception:
            logger.exception("Novus trial progress report failed")

    @run.before_loop
    async def ready(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(90)

async def setup(bot):
    await bot.add_cog(TrialProgressCog(bot))

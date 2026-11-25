"""Private aggregate Council reports; read-only Discord/WOM analytics."""
import asyncio
import io
import datetime as dt
import json
import logging
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import closing
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from common import has_novus_member_role, has_non_clan_role, find_primary_wom_membership
from cogs.tasks_cog import build_wom_headers, get_membership_joined_at
from cogs.application_cog import GUILD, TEST_CHANNEL, RAIDS, PATHWAYS

logger = logging.getLogger("WOMBot")
CHANNEL = 1532825723511767246
COUNCIL = {1532829195657285843,1532830356875513906}
ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
RANKS = ["squire","duellist","inquisitor","expert","knight","paladin"]
DIARY = {"challenger","vanguard","paragon"}
BOSSES = set("""abyssal_sire alchemical_hydra amoxliatl araxxor artio barrows_chests brutus bryophyta callisto calvarion cerberus chambers_of_xeric chambers_of_xeric_challenge_mode chaos_elemental chaos_fanatic commander_zilyana corporeal_beast crazy_archaeologist dagannoth_prime dagannoth_rex dagannoth_supreme deranged_archaeologist doom_of_mokhaiotl duke_sucellus general_graardor giant_mole grotesque_guardians hespori kalphite_queen king_black_dragon kraken kreearra kril_tsutsaroth lunar_chests mad_angel maggot_king mimic nex nightmare phosanis_nightmare obor phantom_muspah sarachnis scorpia scurrius shellbane_gryphon skotizo sol_heredit spindel tempoross the_gauntlet the_corrupted_gauntlet the_fractured_archive the_hueycoatl the_leviathan the_royal_titans the_whisperer theatre_of_blood theatre_of_blood_hard_mode thermonuclear_smoke_devil tombs_of_amascut tombs_of_amascut_expert tzkal_zuk tztok_jad vardorvis venenatis vetion vorkath wintertodt yama zalcano zulrah""".split())
RAID_METRICS = dict(RAIDS + (("ToA Normal","tombs_of_amascut"),))

def now():
    return dt.datetime.now(UTC)
def stamp(value):
    return value.astimezone(UTC).isoformat()
def parse(value):
    try:
        result=dt.datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except (ValueError,TypeError):
        return None
def db():
    return sqlite3.connect("wom_multi.db",timeout=15)
def query(sql,args=()):
    with closing(db()) as conn:
        return conn.execute(sql,args).fetchall()
def init():
    with closing(db()) as conn,conn:
        conn.execute("CREATE TABLE IF NOT EXISTS council_analytics_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT OR IGNORE INTO council_analytics_meta VALUES ('started_at',?)",(stamp(now()),))
        conn.execute("CREATE TABLE IF NOT EXISTS council_roster_snapshots (captured_at TEXT PRIMARY KEY, roster_json TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS council_member_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER NOT NULL, occurred_at TEXT NOT NULL, event TEXT NOT NULL, detail TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS council_application_origins (channel_id INTEGER PRIMARY KEY, applicant_id INTEGER, origin TEXT NOT NULL, recorded_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS council_monthly_reports (report_key TEXT PRIMARY KEY, message_id INTEGER NOT NULL, metrics_json TEXT NOT NULL, sent_at TEXT NOT NULL)")
def roles(member):
    return {r.name.strip().casefold() for r in member.roles}
def full(member):
    return bool(member and not member.bot and has_novus_member_role(member))
def rank(member):
    names=roles(member)
    return next((r for r in reversed(RANKS) if r in names),None)
def month_start(value):
    return value.astimezone(ET).replace(day=1,hour=0,minute=0,second=0,microsecond=0)
def previous_month(value):
    return month_start(value-dt.timedelta(days=1))
def pct(n,d):
    return f"{100*n/d:.1f}% ({n}/{d})" if d else "Not measurable"
def started():
    return parse(query("SELECT value FROM council_analytics_meta WHERE key='started_at'")[0][0])
def events(start,end):
    return query("SELECT member_id,occurred_at,event,detail FROM council_member_events WHERE occurred_at>=? AND occurred_at<?",(stamp(start),stamp(end)))
def snapshot_near(target):
    rows=query("SELECT captured_at,roster_json FROM council_roster_snapshots WHERE captured_at<=? ORDER BY captured_at DESC LIMIT 1",(stamp(target),))
    if not rows or (target-parse(rows[0][0])).total_seconds()>3600*2:
        return None
    return set(json.loads(rows[0][1]))
def coverage_ok(start,end):
    if started()>start:
        return False
    times=[parse(r[0]) for r in query("SELECT captured_at FROM council_roster_snapshots WHERE captured_at>=? AND captured_at<=? ORDER BY captured_at",(stamp(start-dt.timedelta(hours=2)),stamp(end)))]
    times=[t for t in times if t]
    if not times or (times[0]-start).total_seconds()>7200 or (end-times[-1]).total_seconds()>7200:
        return False
    return all((b-a).total_seconds()<=7200 for a,b in zip(times,times[1:]))
def record(member,event,detail=None):
    with closing(db()) as conn,conn:
        conn.execute("INSERT INTO council_member_events(member_id,occurred_at,event,detail) VALUES (?,?,?,?)",(member,stamp(now()),event,detail))

class CouncilAnalyticsCog(commands.Cog):
    def __init__(self,bot):
        self.bot=bot
        self.last_snapshot=None
        self.tick.start()
    def cog_unload(self):
        self.tick.cancel()
    def council_channel(self,guild):
        channel=guild.get_channel(CHANNEL)
        if not isinstance(channel,discord.TextChannel) or channel.name.casefold()!="council-chat" or channel.permissions_for(guild.default_role).view_channel:
            return None
        for role in guild.roles:
            if role.id not in COUNCIL and not role.is_bot_managed() and not role.permissions.administrator and channel.permissions_for(role).view_channel:
                return None
        for target,overwrite in channel.overwrites.items():
            if isinstance(target,discord.Member) and not target.bot and overwrite.view_channel is True and not any(r.id in COUNCIL for r in target.roles) and not target.guild_permissions.administrator:
                return None
        return channel
    def roster(self,guild):
        return {m.id:m for m in guild.members if full(m)}
    def capture(self,guild):
        t=now()
        if self.last_snapshot and (t-self.last_snapshot).total_seconds()<14*60:
            return
        if not guild.chunked:
            return
        with closing(db()) as conn,conn:
            conn.execute("INSERT OR IGNORE INTO council_roster_snapshots VALUES (?,?)",(stamp(t),json.dumps(sorted(self.roster(guild)))))
        self.last_snapshot=t
    @commands.Cog.listener()
    async def on_member_update(self,before,after):
        if after.guild.id!=GUILD or after.bot:
            return
        try:
            b,a=full(before),full(after)
            if b!=a:
                record(after.id,"joined" if a else "departed")
            if a and b:
                old,new=rank(before),rank(after)
                if old and new and RANKS.index(new)>RANKS.index(old):
                    record(after.id,"promotion",new)
                for role in (roles(after)-roles(before))&DIARY:
                    record(after.id,"diary",role)
        except Exception:
            logger.exception("Council analytics member event failed")
    @commands.Cog.listener()
    async def on_member_remove(self,member):
        if member.guild.id==GUILD and full(member):
            try:
                record(member.id,"departed","server departure")
            except Exception:
                logger.exception("Council analytics departure failed")
    @commands.Cog.listener()
    async def on_member_join(self,member):
        if member.guild.id==GUILD and full(member):
            try:
                record(member.id,"joined","server join")
            except Exception:
                logger.exception("Council analytics join failed")
    async def wom(self,group_id,endpoint="",params=None):
        try:
            async with self.bot.http_session.get(f"https://api.wiseoldman.net/v2/groups/{group_id}"+endpoint,params=params,headers=build_wom_headers(),timeout=60) as response:
                if response.status!=200:
                    logger.warning("Council WOM %s status=%s",endpoint,response.status)
                    return None
                return await response.json()
        except Exception:
            logger.exception("Council WOM request failed %s",endpoint)
            return None
    async def activity(self,group,start,end):
        result=[]
        seen=set()
        complete=False
        for page in range(100):
            rows=await self.wom(group,"/activity",{"limit":50,"offset":page*50})
            if not isinstance(rows,list):
                break
            if not rows:
                complete=True
                break
            dates=[]
            for row in rows:
                if not isinstance(row,dict):
                    continue
                when=parse(row.get("createdAt"))
                if not when:
                    continue
                dates.append(when)
                key=(row.get("playerId"),row.get("type"),row.get("role"),row.get("createdAt"))
                if key not in seen and start<=when<end:
                    seen.add(key)
                    result.append(row)
            if dates and min(dates)<start:
                complete=True
                break
            if len(rows)<50:
                complete=True
                break
            await asyncio.sleep(0.2)
        return Counter(r.get("type") for r in result),complete
    def recruitment(self,start,end):
        rows=query("""SELECT r.channel_id,r.applicant_id,r.message_id,o.origin,a.pathway,a.approved_at
            FROM application_kc_reports r
            LEFT JOIN council_application_origins o ON o.channel_id=r.channel_id AND o.applicant_id=r.applicant_id
            LEFT JOIN application_final_approvals a ON a.channel_id=r.channel_id AND a.message_id=r.message_id
            WHERE r.channel_id!=? AND r.applicant_id IS NOT NULL""",(TEST_CHANNEL,))
        opened=approved=converted=unknown=0
        pathways=Counter()
        for channel,applicant,message,origin,pathway,approved_at in rows:
            created=discord.utils.snowflake_time(message)
            approval=parse(approved_at)
            in_window=start<=created<end
            approval_window=bool(approval and start<=approval<end)
            if origin not in ("new","trial"):
                if origin is None and (in_window or approval_window):
                    unknown+=1
                continue
            if in_window:
                opened+=1
                if approval and approval<end:
                    converted+=1
            if approval_window:
                approved+=1
                pathways[pathway]+=1
        return opened,approved,converted,pathways,unknown
    def trial_stats(self,start,end):
        rows=query("SELECT discord_id,started_at,entry_source,ended_at,outcome FROM trial_analytics_episodes WHERE guild_id=?",(GUILD,))
        approvals=query("""SELECT r.applicant_id,a.pathway,a.approved_at FROM application_final_approvals a
            JOIN application_kc_reports r ON r.channel_id=a.channel_id AND r.message_id=a.message_id
            WHERE r.applicant_id IS NOT NULL AND r.channel_id!=?""",(TEST_CHANNEL,))
        by_member=defaultdict(list)
        for member,pathway,t in approvals:
            when=parse(t)
            if when:
                by_member[member].append((when,pathway))
        active=new=late=passed=other=unknown=departed=0
        durations=[]
        for member,began,source,ended,outcome in rows:
            b,e=parse(began),parse(ended)
            if not b or b>=end:
                continue
            if not e or e>=end:
                active+=1
            if start<=b<end:
                if source=="role_event":
                    new+=1
                else:
                    late+=1
            if not e or not start<=e<end:
                continue
            matches=[p for t,p in by_member[member] if b<=t and abs((t-e).total_seconds())<=900]
            if outcome in ("passed","promoted_unconfirmed"):
                if matches and matches[-1]=="raidtrial":
                    passed+=1
                    if source=="role_event":
                        durations.append((e-b).total_seconds()/86400)
                elif matches:
                    other+=1
                else:
                    unknown+=1
            else:
                departed+=1
        vouches=sum(1 for (v,) in query("SELECT created_at FROM trial_vouches WHERE guild_id=?",(GUILD,)) if parse(v) and start<=parse(v)<end)
        return active,new,late,passed,other,unknown,departed,vouches,durations
    async def build(self,guild,start,end,key,lifetime=False):
        members=self.roster(guild)
        trial_now=sum(1 for m in guild.members if not m.bot and not has_non_clan_role(m) and "trial member" in roles(m))
        configs=query("SELECT group_id FROM guild_configs WHERE guild_id=?",(GUILD,))
        group_id=configs[0][0] if configs else None
        group=await self.wom(group_id) if group_id else None
        if not isinstance(group,dict) or not isinstance(group.get("memberships"),list) or not group["memberships"]:
            raise RuntimeError("WOM roster unavailable; report not posted")
        roster=group["memberships"]
        group_created=parse(group.get("createdAt"))
        joined_dates=[get_membership_joined_at(m) for m in roster]
        joined_dates=[t for t in joined_dates if t and t<=end]
        dates=[t for t in [group_created]+joined_dates if t and t<=end]
        if lifetime:
            if not dates:
                raise RuntimeError("No verified lifetime start date")
            start=min(dates)
        links=query("SELECT discord_id,rsn,wom_id FROM links WHERE guild_id=?",(GUILD,))
        alts=query("SELECT discord_id,rsn,wom_id FROM alt_links WHERE guild_id=?",(GUILD,))
        owners={}
        tenures=[]
        linked=set()
        for member,rsn,pid in links:
            if member not in members:
                continue
            match=find_primary_wom_membership(rsn,pid,roster)
            if not match:
                continue
            player=match.get("player",{}).get("id")
            if player is None:
                continue
            linked.add(member)
            owners[player]=member
            joined=get_membership_joined_at(match)
            if joined and joined<=now():
                tenures.append((now()-joined).days)
        for member,rsn,pid in alts:
            if member not in linked:
                continue
            match=find_primary_wom_membership(rsn,pid,roster)
            if match and match.get("player",{}).get("id") is not None:
                owners.setdefault(match["player"]["id"],member)
        gains=await self.wom(group_id,"/bulk-gained",{"startDate":stamp(start),"endDate":stamp(end)})
        account_activity,activity_complete=await self.activity(group_id,start,end)
        ev=events(start,end)
        kinds=Counter(e[2] for e in ev)
        promo=Counter(e[3] for e in ev if e[2]=="promotion")
        diary=Counter(e[3] for e in ev if e[2]=="diary")
        start_set=snapshot_near(start)
        end_set=set(members) if lifetime else snapshot_near(end)
        complete=coverage_ok(start,end) and start_set is not None and end_set is not None
        opened,approved,converted,paths,unknown_apps=self.recruitment(start,end)
        active,new,late,passed,other,unknown,departed,vouches,durations=self.trial_stats(start,end)
        kc=Counter()
        observed=set()
        active_players=set()
        covered_accounts=set()
        first=Counter()
        if isinstance(gains,list):
            seen=set()
            for entry in gains:
                if not isinstance(entry,dict):
                    continue
                pid=entry.get("player",{}).get("id")
                if pid not in owners or pid in seen:
                    continue
                a,b=parse(entry.get("startDate")),parse(entry.get("endDate"))
                if not a or not b or a>=b:
                    continue
                seen.add(pid)
                for metric in entry.get("data",[]):
                    if not isinstance(metric,dict):
                        continue
                    name=metric.get("metric")
                    x,y,g=metric.get("start"),metric.get("end"),metric.get("gained")
                    if name not in BOSSES or type(x) is not int or type(y) is not int or type(g) is not int or min(x,y)<0 or g<0:
                        continue
                    observed.add(name)
                    covered_accounts.add(pid)
                    kc[name]+=g
                    if g>0:
                        active_players.add(owners[pid])
                    if name in ("tzkal_zuk","sol_heredit") and x==0 and y>=1:
                        first[name]+=1
        measured_members={owners[p] for p in covered_accounts}
        rank_counts=Counter(rank(m) or "unranked" for m in members.values())
        label=lambda t:t.astimezone(ET).strftime("%b %d, %Y")
        # End is exclusive for the calendar report.
        period=f"{label(start)} to {label(end if lifetime else end-dt.timedelta(seconds=1))}"
        embed=discord.Embed(title="Monthly Novus Report | Lifetime Edition" if lifetime else f"Monthly Novus Report | {start.astimezone(ET):%B %Y}",description=f"**{period}**\n{'Earliest verified WOM/clan records to date.' if lifetime else 'Previous calendar month.'}\nCouncil only • Aggregate metrics",color=discord.Color.blurple(),timestamp=now())
        embed.set_thumbnail(url=(guild.icon or self.bot.user.display_avatar).url)
        def field(name,value):
            embed.add_field(name=name,value=value[:1024],inline=False)
        membership=f"**Full Members:** {len(members)} • **Trial Members:** {trial_now}\n**WOM accounts:** {len(roster)} • **Linked full members:** {len(linked)}"
        membership+="\n**Period-end full members:** "+(str(len(end_set)) if end_set is not None else "Unavailable")
        membership+=f"\n**Observed member joins / departures:** {kinds['joined']} / {kinds['departed']}"+("" if complete else " (partial history)")
        membership+=f"\n**Net member growth:** {len(end_set)-len(start_set):+d}" if complete else "\n**Net member growth:** baseline establishing"
        membership+=f"\n**WOM account joins / leaves:** {account_activity['joined']} / {account_activity['left']}"+("" if activity_complete else " (partial)")
        field("Membership Overview",membership)
        retention=(f"**Period retention:** {pct(len(start_set&end_set),len(start_set))}" if complete else "**Period retention:** baseline establishing")
        if tenures:
            retention+=f"\n**Current in-game stint:** mean {statistics.mean(tenures):.0f}d • median {statistics.median(tenures):.0f}d ({len(tenures)} known)"
        else:
            retention+="\n**Current stint:** unavailable"
        for days in (30,60,90):
            target=end-dt.timedelta(days=days)
            base=snapshot_near(target)
            value=pct(len(base&end_set),len(base)) if base is not None and end_set is not None and coverage_ok(target,end) else "Baseline establishing"
            retention+=f"\n**{days}-day roster retention:** {value}"
        field("Member Retention",retention)
        recruitment=f"**Classified applications / approvals:** {opened} / {approved}\n**Application cohort approved by cutoff:** {pct(converted,opened)}\n**Historical unclassified tickets excluded:** {unknown_apps}\n"
        recruitment+="\n".join(f"{PATHWAYS[p]}: {paths[p]}" for p in PATHWAYS)
        recruitment+="\nStaff tests/existing members excluded where known; manual approvals not included."
        field("Recruitment",recruitment)
        t=f"**Active tracked trials at cutoff:** {active}\n**New / late-observed trials:** {new} / {late}\n**Vouch-based / other-path graduations:** {passed} / {other}\n**Unclassified graduations:** {unknown}\n**Closed without graduation:** {departed} (reason unknown)\n**Vouches recorded:** {vouches}\n**Confirmed vouch graduation share of classified closures:** {pct(passed,passed+other+departed) if passed+other+departed and not unknown else 'Not yet measurable'}"
        t+="\n**Median confirmed trial duration:** "+(f"{statistics.median(durations):.1f} days" if durations else "Not yet measurable")
        field("Raid Trial Analytics",t)
        raid_total=sum(kc[m] for m in RAID_METRICS.values())
        if covered_accounts:
            p=f"**Observed raid gains:** {raid_total:,} KC\n"+"\n".join(f"{name}: {kc[metric]:,}" if metric in observed else f"{name}: N/A" for name,metric in RAID_METRICS.items())
            top=[(m,n) for m,n in kc.most_common() if m not in RAID_METRICS.values() and n>0][:5]
            if top:
                p+="\n**Top non-raid bosses:** "+", ".join(f"{m.replace('_',' ').title()} {n:,}" for m,n in top)
            p+="\nCurrent linked in-clan accounts; gains can predate joining."
        else:
            p="WOM gains unavailable or insufficient comparable snapshots."
        field("PvM Activity",p)
        field("Member Achievements",f"**Observed first-KC transitions (0 to 1+):** Inferno {first['tzkal_zuk']:,} / Colosseum {first['sol_heredit']:,}" if covered_accounts else "**First-KC transitions:** unavailable")
        last=embed.fields[-1].value
        embed.set_field_at(len(embed.fields)-1,name="Member Achievements",value=last+f"\n**PvM Diary role awards:** {sum(diary.values())} observed\n**Approved PBs:** Separate bot data unavailable\nFirst-KC counts are lower bounds, not a complete achievement history.",inline=False)
        field("Tenure Progression",f"**Observed promotions:** {sum(promo.values())}"+("" if complete else " (partial history)")+"\n**Current ranks:** "+" • ".join(f"{r.title()} {rank_counts[r]}" for r in RANKS)+f"\nNo tenure rank: {rank_counts['unranked']}")
        engagement=(f"**Members with positive boss/raid gains:** {pct(len(active_players),len(measured_members))}\n**No positive observed gains:** {len(measured_members-active_players)}\n**No comparable data:** {len(set(members)-measured_members)}\n**Account coverage:** {len(covered_accounts)}/{len(owners)} linked accounts" if covered_accounts else "WOM gain coverage unavailable.")
        field("Member Engagement",engagement)
        metrics={"full":len(end_set) if end_set is not None else None,"raid_kc":raid_total if covered_accounts else None,"activity_members":len(active_players) if covered_accounts else None,"measured":len(measured_members),"coverage_accounts":len(covered_accounts),"owners":len(owners),"joins":kinds["joined"] if complete else None,"departures":kinds["departed"] if complete else None,"approved":approved if complete else None,"trial_passed":passed if complete and not unknown else None}
        previous_key=previous_month(start).strftime("%Y-%m") if not lifetime else None
        prior=query("SELECT metrics_json FROM council_monthly_reports WHERE report_key=?",(previous_key,)) if previous_key else []
        if prior:
            old=json.loads(prior[0][0])
            comparisons=[]
            for key_,title in (("full","Full members"),("raid_kc","Observed raid KC"),("joins","Member joins"),("departures","Member departures"),("approved","Classified approvals"),("trial_passed","Confirmed raid trial graduations")):
                a,b=metrics.get(key_),old.get(key_)
                comparisons.append(f"{title}: {a-b:+,}" if a is not None and b is not None else f"{title}: not comparable")
            comparisons.append("PvM totals reflect each report's available account coverage, not a fixed cohort.")
            field("Month-over-Month Comparison","\n".join(comparisons))
        else:
            field("Month-over-Month Comparison","Baseline report. Regular monthly comparisons begin once two complete comparable reports exist.")
        field("Data Coverage",f"Prospective roster tracking began {label(started())}. Historical member events and trial outcomes are incomplete. WOM gains cover current linked full-member accounts only; departed accounts and unranked metrics are not reconstructed. Current headcounts/ranks are as of report generation.")
        embed.set_footer(text=f"Novus Analytics • Council only • {key}")
        if len(embed)>5900:
            raise RuntimeError("Council report exceeds embed budget")
        chart_data={
            "period":period,"lifetime":lifetime,"full":len(members),"trial":trial_now,
            "wom":len(roster),"linked":len(linked),"tenures":tenures,
            "joins":kinds["joined"],"departures":kinds["departed"],"complete":complete,
            "wom_joins":account_activity["joined"],"wom_leaves":account_activity["left"],"wom_complete":activity_complete,
            "retention":(100*len(start_set&end_set)/len(start_set)) if complete and start_set else None,
            "opened":opened,"approved":approved,"converted":converted,"unknown_apps":unknown_apps,
            "paths":dict(paths),"active":active,"new":new,"late":late,"passed":passed,"other":other,
            "unknown":unknown,"departed":departed,"vouches":vouches,
            "duration":statistics.median(durations) if durations else None,
            "raids":{name:kc[metric] if metric in observed else None for name,metric in RAID_METRICS.items()},
            "bosses":{m.replace("_"," ").title():n for m,n in kc.most_common() if m not in RAID_METRICS.values() and n>0},
            "first":dict(first),"diary":sum(diary.values()),"promotions":dict(promo),
            "ranks":dict(rank_counts),"engaged":len(active_players),"measured":len(measured_members),
            "unmeasured":len(set(members)-measured_members),"covered":len(covered_accounts),"owners":len(owners),
            "prior":json.loads(prior[0][0]) if prior else None,"metrics":metrics
        }
        return embed,metrics,chart_data

    async def deliver(self,guild,channel,start,end,key,lifetime=False):
        saved=query("SELECT message_id,sent_at FROM council_monthly_reports WHERE report_key=?",(key,))
        if saved:
            # Restyle the first text-only lifetime report in place, once.
            if not lifetime:
                return
            try:
                existing=await channel.fetch_message(saved[0][0])
            except discord.NotFound:
                return
            if existing.author.id!=self.bot.user.id or not existing.embeds or (any(a.filename=="novus-membership.png" for a in existing.attachments) and not existing.embeds[0].fields):
                return
            cutoff=parse(saved[0][1]) or end
            embed,metrics,chart_data=await self.build(guild,start,cutoff,key,lifetime)
            images=await asyncio.to_thread(render_dashboards,chart_data)
            embed,chart_embeds,files=dashboard_message(embed,images,chart_data)
            if self.council_channel(guild)!=channel:
                return
            await existing.edit(embeds=[embed]+chart_embeds,attachments=files,allowed_mentions=discord.AllowedMentions.none())
            with closing(db()) as conn,conn:
                conn.execute("UPDATE council_monthly_reports SET metrics_json=? WHERE report_key=?",(json.dumps(metrics),key))
            logger.info("Council lifetime report updated with charts message=%s",existing.id)
            return
        # Durable recovery: a successful Discord send before a DB commit is
        # recognized by the exact bot-owned footer, never sent twice.
        marker=f"Novus Analytics • Council only • {key}"
        async for message in channel.history(limit=500):
            if message.author.id==self.bot.user.id and any(e.footer.text==marker for e in message.embeds):
                with closing(db()) as conn,conn:
                    conn.execute("INSERT OR IGNORE INTO council_monthly_reports VALUES (?,?,?,?)",(key,message.id,"{}",stamp(message.created_at)))
                logger.info("Council report recovered key=%s message=%s",key,message.id)
                return
        embed,metrics,chart_data=await self.build(guild,start,end,key,lifetime)
        images=await asyncio.to_thread(render_dashboards,chart_data)
        embed,chart_embeds,files=dashboard_message(embed,images,chart_data)
        if self.council_channel(guild)!=channel:
            raise RuntimeError("Council channel privacy changed")
        message=await channel.send(embeds=[embed]+chart_embeds,files=files,allowed_mentions=discord.AllowedMentions.none())
        logger.info("Council report posted key=%s channel=%s message=%s metrics=%s",key,channel.id,message.id,json.dumps(metrics,sort_keys=True))
        with closing(db()) as conn,conn:
            conn.execute("INSERT OR IGNORE INTO council_monthly_reports VALUES (?,?,?,?)",(key,message.id,json.dumps(metrics),stamp(now())))
    @tasks.loop(minutes=15)
    async def tick(self):
        try:
            guild=self.bot.get_guild(GUILD)
            if not guild:
                return
            if not guild.chunked:
                await guild.chunk(cache=True)
            if not guild.chunked:
                return
            self.capture(guild)
            channel=self.council_channel(guild)
            if channel is None:
                logger.warning("Council analytics skipped: council-only channel not verified")
                return
            t=now()
            await self.deliver(guild,channel,None,t,"lifetime-baseline-2026",lifetime=True)
            local=t.astimezone(ET)
            boundary=month_start(local)
            if boundary>=dt.datetime(2026,12,1,tzinfo=ET) and local>=boundary.replace(hour=12):
                start=previous_month(boundary)
                await self.deliver(guild,channel,start,boundary,start.strftime("%Y-%m"))
        except Exception:
            logger.exception("Council analytics report cycle failed")
    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(25)


def render_dashboards(d):
    # Local rendering: no member data is sent to a third-party chart service.
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    BG="#10151f"; PANEL="#192231"; FG="#f0f4fc"; MUTED="#a5b2c5"
    BLUE="#8197fa"; GREEN="#4ad6a1"; GOLD="#e7bb68"; RED="#e8828a"
    def board(title,subtitle):
        fig,axs=plt.subplots(3,2,figsize=(12,13.5),facecolor=BG)
        fig.subplots_adjust(left=.16,right=.93,bottom=.08,top=.86,wspace=.43,hspace=.58)
        fig.text(.055,.957,"NOVUS  /  COUNCIL ANALYTICS",color=GREEN,fontsize=12,weight="bold")
        fig.text(.055,.925,title,color=FG,fontsize=25,weight="bold")
        fig.text(.055,.895,d["period"]+"  •  "+subtitle,color=MUTED,fontsize=12)
        return fig,axs.ravel()
    def prepare(ax,title):
        ax.set_facecolor(PANEL)
        ax.set_title(title,loc="left",color=FG,fontsize=15,weight="bold",pad=12)
        for sp in ax.spines.values():sp.set_visible(False)
        ax.tick_params(colors=MUTED,labelsize=10,length=0)
        ax.set_axisbelow(True)
    def empty(ax,title,message):
        prepare(ax,title)
        ax.set_xticks([]);ax.set_yticks([])
        ax.text(.5,.5,message,ha="center",va="center",wrap=True,color=MUTED,fontsize=12,transform=ax.transAxes)
    def bars(ax,title,pairs,colors=BLUE,note=None):
        valid=[(label,value) for label,value in pairs if value is not None]
        if not valid:
            return empty(ax,title,"No comparable data yet")
        prepare(ax,title)
        labels,values=zip(*valid)
        ax.barh(list(range(len(values))),values,color=colors,height=.64)
        ax.set_yticks(list(range(len(values))),labels=labels)
        ax.invert_yaxis()
        ax.set_xlim(0,max(max(values)*1.25,1))
        ax.xaxis.set_major_formatter(ticker.StrMethodFormatter("{x:,.0f}"))
        ax.grid(axis="x",alpha=.12,color=MUTED)
        for i,value in enumerate(values):
            ax.text(value+max(max(values),1)*.025,i,f"{value:,.0f}",color=FG,va="center",fontsize=11,weight="bold")
        if note:ax.text(0,-.22,note,color=MUTED,fontsize=9,transform=ax.transAxes,wrap=True)
    def save(fig,footer):
        fig.text(.055,.035,footer,color=MUTED,fontsize=10,wrap=True)
        buf=io.BytesIO();fig.savefig(buf,format="png",dpi=125,facecolor=BG)
        plt.close(fig);buf.seek(0)
        return buf
    f,a=board("Membership & recruitment","Roster, retention and trial outcomes")
    bars(a[0],"Current membership",[("Full members",d["full"]),("Trial members",d["trial"]),("WOM accounts",d["wom"]),("Linked members",d["linked"])])
    if d["wom_complete"] or d["wom_joins"] or d["wom_leaves"]:
        bars(a[1],"WOM account movements",[("Joined",d["wom_joins"]),("Left",d["wom_leaves"])],[GREEN,RED],"Accounts, not unique people." + ("" if d["wom_complete"] else " Partial history."))
    else:empty(a[1],"WOM account movements","Historical account activity unavailable")
    if d["retention"] is not None:
        bars(a[2],"Roster retention",[("Retained (%)",d["retention"])],GREEN)
        a[2].set_xlim(0,110)
    elif d["tenures"]:
        bars(a[2],"Current membership tenure",[("Mean days",statistics.mean(d["tenures"])),("Median days",statistics.median(d["tenures"]))],GOLD,"Historical retention baseline establishing.")
    else:empty(a[2],"Member retention","Baseline establishing")
    if d["opened"] or d["approved"]:
        bars(a[3],"Classified recruitment",[("Applications",d["opened"]),("Approvals",d["approved"])],BLUE,f'{d["unknown_apps"]} historical tickets unclassified.')
    else:empty(a[3],"Recruitment",f'Baseline establishing\n{d["unknown_apps"]} historical tickets unclassified')
    bars(a[4],"Trial outcomes (tracked)",[("Active",d["active"]),("Vouch graduation",d["passed"]),("Other pathway",d["other"]),("Unclassified",d["unknown"]),("Closed",d["departed"])],BLUE,"Observed episodes; incomplete historical coverage.")
    bars(a[5],"Current tenure ranks",[(r.title(),d["ranks"].get(r,0)) for r in RANKS]+[("Unranked",d["ranks"].get("unranked",0))],GREEN)
    one=save(f,"Current membership is a snapshot. Historical events are partial; staff test applications are excluded where known.")
    f,a=board("PvM & member progression","Observed gains and achievements")
    bars(a[0],"Raid KC gained",list(d["raids"].items()),GREEN,"Only comparable WOM metrics shown.")
    bars(a[1],"Top non-raid bosses",list(d["bosses"].items())[:5],BLUE)
    if d["covered"]:
        bars(a[2],"Member PvM engagement",[("Positive gains",d["engaged"]),("No positive gains",d["measured"]-d["engaged"]),("No comparable data",d["unmeasured"])],[GREEN,GOLD,MUTED],f'{d["covered"]}/{d["owners"]} linked accounts measured.')
        bars(a[3],"First KC transitions",[("Inferno",d["first"].get("tzkal_zuk",0)),("Colosseum",d["first"].get("sol_heredit",0))],GREEN,"Observed 0 to 1+ transitions, lower bound.")
    else:
        empty(a[2],"Member PvM engagement","Comparable WOM snapshots unavailable")
        empty(a[3],"First KC transitions","Comparable WOM snapshots unavailable")
    bars(a[4],"Observed tenure promotions",[(r.title(),d["promotions"].get(r,0)) for r in RANKS[1:]],BLUE,"Partial history until full monthly coverage.")
    if d["prior"]:
        pairs=[(label,d["metrics"].get(k)-d["prior"].get(k)) for k,label in (("full","Full members"),("joins","Member joins"),("approved","Approvals")) if d["metrics"].get(k) is not None and d["prior"].get(k) is not None]
        if pairs:
            prepare(a[5],"Month-over-month change")
            labels,values=zip(*pairs);a[5].barh(range(len(values)),values,color=[GREEN if v>=0 else RED for v in values])
            a[5].set_yticks(range(len(values)),labels=labels);a[5].invert_yaxis();a[5].axvline(0,color=MUTED,alpha=.5)
        else:empty(a[5],"Month-over-month","No comparable prior period")
    else:empty(a[5],"Month-over-month","Baseline edition\nMonthly comparison begins with comparable reports")
    two=save(f,"WOM gains describe current linked accounts and may predate joining. Missing metrics are not treated as zero.")
    return [one,two]

def dashboard_message(embed,images,d):
    # A short header, charts immediately below, then only supplemental notes.
    notes=discord.Embed(title="Key context",color=discord.Color.dark_embed())
    retention=next((f.value for f in embed.fields if f.name=="Member Retention"),"")
    horizons=[line for line in retention.splitlines() if "-day roster retention:" in line]
    if horizons:
        notes.add_field(name="Retention",value="\n".join(horizons),inline=False)
    paths=" • ".join(f"{PATHWAYS[p]}: {n}" for p,n in d["paths"].items() if p in PATHWAYS and n)
    notes.add_field(name="Recruitment & trials",value=(
        f'Application cohort approved: {pct(d["converted"],d["opened"])}\n'
        +(paths+"\n" if paths else "")
        +f'Vouches recorded: {d["vouches"]} • Median confirmed trial: '
        +(f'{d["duration"]:.1f} days' if d["duration"] is not None else "Not measurable")
        +f'\nHistorical unclassified applications: {d["unknown_apps"]}'
    ),inline=False)
    notes.add_field(name="Achievements",value=f'Observed PvM Diary awards: {d["diary"]} • Approved PBs: not integrated',inline=False)
    notes.description="Historical gaps are not zero activity. Current linked-account WOM gains may predate joining; departed accounts are not reconstructed. Retention and event tracking are prospective."
    embed.clear_fields()
    embed.description=f'**{d["period"]}**\n**{d["full"]}** full members • **{d["trial"]}** trial members • **{d["wom"]}** WOM accounts\n'+("Lifetime baseline" if d["lifetime"] else "Previous calendar month")+" • Council only"
    names=["novus-membership.png","novus-pvm.png"]
    charts=[]
    files=[]
    for name,image in zip(names,images):
        card=discord.Embed(color=discord.Color.blurple())
        card.set_image(url="attachment://"+name)
        charts.append(card)
        files.append(discord.File(image,filename=name))
    return embed,charts+[notes],files

async def setup(bot):
    init()
    await bot.add_cog(CouncilAnalyticsCog(bot))

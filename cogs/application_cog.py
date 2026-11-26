"""Novus applicant KC screening and staff gear review, separate from approval and tenure."""
import asyncio
import datetime
from zoneinfo import ZoneInfo
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from urllib.parse import quote

import discord
from novus_logging import check, notice, bind_novus_ui
from discord.ext import commands, tasks

from common import WOM_API_KEY, sanitize_rsn
from rpp_progress import RPP_KC, rpp_progress
from cogs.vouches_cog import get_vouches, add_vouch_feedback, REQUIRED_VOUCHES, is_trial

logger = logging.getLogger("WOMBot")
GUILD = 1532817291048124466
CATEGORY = 1560643746188632205
TEST_CHANNEL = 1575015372502081546
STAFF = {1532829195657285843, 1532830356875513906, 1532830960062693396, 1533587986204069908}
RAIDS = (
    ("CoX", "chambers_of_xeric"),
    ("CoX CM", "chambers_of_xeric_challenge_mode"),
    ("ToB", "theatre_of_blood"),
    ("ToB HM", "theatre_of_blood_hard_mode"),
    ("ToA Expert", "tombs_of_amascut_expert"),
    ("TFA", "the_fractured_archive"),
)
EXTRAS = (("Inferno", "tzkal_zuk"), ("Colosseum", "sol_heredit"))
TRIAL_REFERENCE = (
    ("Doom of Mokhaiotl", "doom_of_mokhaiotl"),
    ("Phosani's Nightmare", "phosanis_nightmare"),
)
REPORT_TITLE = "✦ Novus Applicant Eligibility"
REPORT_TITLES = (REPORT_TITLE, "Novus Applicant Eligibility")
DETAILS_TITLE = "✦ Novus Application Details"
DETAILS_TITLES = (DETAILS_TITLE, "Novus Application Details")

APPLICATION_INTRO = """## Thanks for applying to Novus!

Click **Complete Application** to provide your account type, membership option, gear screenshots and how you found us.

-# Your raid KC is retrieved automatically through Wise Old Man."""
APPLICATION_SUBMITTED = "**Application details submitted.** Staff will review your application and gear screenshots. Add any further information in this ticket."


def db():
    return sqlite3.connect("wom_multi.db")

def initialize():
    with closing(db()) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS application_kc_cache (guild_id INTEGER, discord_id INTEGER, rsn TEXT, payload TEXT, PRIMARY KEY(guild_id, discord_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS application_kc_reports (channel_id INTEGER PRIMARY KEY, applicant_id INTEGER, rsn TEXT NOT NULL, message_id INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS application_gear_reviews (channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, reviewer_id INTEGER NOT NULL, verified_at TEXT NOT NULL, PRIMARY KEY(channel_id, message_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS application_final_approvals (channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL, reviewer_id INTEGER NOT NULL, pathway TEXT NOT NULL, approved_at TEXT NOT NULL, PRIMARY KEY(channel_id, message_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS application_trial_graduations (episode_id INTEGER PRIMARY KEY, applicant_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, report_id INTEGER NOT NULL, pathway TEXT NOT NULL, approved_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS application_message_tests (test_key TEXT PRIMARY KEY, message_id INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS application_form_submissions (channel_id INTEGER NOT NULL, report_id INTEGER NOT NULL, applicant_id INTEGER NOT NULL, response_id INTEGER NOT NULL, submitted_at TEXT NOT NULL, PRIMARY KEY(channel_id, report_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS application_form_prompts (channel_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS application_welcome_delivery (channel_id INTEGER NOT NULL, report_id INTEGER NOT NULL, welcome_id INTEGER, PRIMARY KEY(channel_id, report_id))")

def form_submission(channel_id, report_id):
    with closing(db()) as conn:
        return conn.execute("SELECT applicant_id, response_id FROM application_form_submissions WHERE channel_id=? AND report_id=?", (channel_id, report_id)).fetchone()

def save_form_submission(channel_id, report_id, applicant_id, response_id):
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO application_form_submissions VALUES (?, ?, ?, ?, ?)", (channel_id, report_id, applicant_id, response_id, datetime.datetime.now(datetime.timezone.utc).isoformat()))

def get_form_prompt(channel_id):
    with closing(db()) as conn:
        row = conn.execute("SELECT message_id FROM application_form_prompts WHERE channel_id=?", (channel_id,)).fetchone()
    return row[0] if row else None

def save_form_prompt(channel_id, message_id):
    with closing(db()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO application_form_prompts VALUES (?, ?)", (channel_id, message_id))

def form_content(channel_id, report_id):
    if get_form_prompt(channel_id):
        return None
    return APPLICATION_SUBMITTED if form_submission(channel_id, report_id) else APPLICATION_INTRO

def get_report(channel_id):
    with closing(db()) as conn:
        return conn.execute("SELECT applicant_id, rsn, message_id FROM application_kc_reports WHERE channel_id=?", (channel_id,)).fetchone()

def save_report(channel_id, applicant_id, rsn, message_id):
    with closing(db()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO application_kc_reports VALUES (?, ?, ?, ?)", (channel_id, applicant_id, rsn, message_id))

def gear_reviewer(channel_id, message_id):
    with closing(db()) as conn:
        row = conn.execute("SELECT reviewer_id FROM application_gear_reviews WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone()
    return row[0] if row else None

def set_gear_review(channel_id, message_id, reviewer_id):
    with closing(db()) as conn, conn:
        if reviewer_id is None:
            conn.execute("DELETE FROM application_gear_reviews WHERE channel_id=? AND message_id=?", (channel_id, message_id))
        else:
            conn.execute("INSERT OR REPLACE INTO application_gear_reviews VALUES (?, ?, ?, ?)", (channel_id, message_id, reviewer_id, datetime.datetime.now(datetime.timezone.utc).isoformat()))

def gear_status(reviewer_id):
    return f"{check()} **Gear requirement met** (verified by <@{reviewer_id}>)" if reviewer_id else "▫️ Gear verification pending"

PATHWAYS = {
    "standard": "200+ total raid KC",
    "alternative": "100+ raid KC + Inferno/Colosseum",
    "rpp": "Raids Prep Program completed",
    "raidtrial": "Raid Trial completed (3 member vouches)",
}

def final_approval(channel_id, message_id):
    with closing(db()) as conn:
        return conn.execute("SELECT reviewer_id, pathway FROM application_final_approvals WHERE channel_id=? AND message_id=?", (channel_id, message_id)).fetchone()

def save_final_approval(channel_id, message_id, reviewer_id, pathway, *, trial_episode_id=None, applicant_id=None):
    with closing(db()) as conn, conn:
        conn.execute("INSERT INTO application_final_approvals VALUES (?, ?, ?, ?, ?)", (channel_id, message_id, reviewer_id, pathway, datetime.datetime.now(datetime.timezone.utc).isoformat()))
        conn.execute("INSERT OR IGNORE INTO application_welcome_delivery VALUES (?, ?, NULL)", (channel_id, message_id))
        if trial_episode_id is not None and applicant_id is not None:
            conn.execute("INSERT OR IGNORE INTO application_trial_graduations VALUES (?, ?, ?, ?, ?, ?)",
                         (trial_episode_id, applicant_id, channel_id, message_id, pathway, datetime.datetime.now(datetime.timezone.utc).isoformat()))

def pending_deliveries():
    with closing(db()) as conn:
        return conn.execute("SELECT channel_id, report_id FROM application_welcome_delivery WHERE welcome_id IS NULL").fetchall()

def delivered_welcome(channel_id, report_id):
    with closing(db()) as conn:
        row = conn.execute("SELECT welcome_id FROM application_welcome_delivery WHERE channel_id=? AND report_id=?", (channel_id, report_id)).fetchone()
    return row[0] if row else None

def mark_welcome_delivered(channel_id, report_id, message_id):
    with closing(db()) as conn, conn:
        conn.execute("UPDATE application_welcome_delivery SET welcome_id=? WHERE channel_id=? AND report_id=?", (message_id, channel_id, report_id))

WELCOME_TEXT = """## Welcome to Novus!

**🌍 Home World** • 346
⠀
**Looking for a Group**
Grab PvM roles and pings in {assign_roles}, or start your own group in {create_lfg}.
⠀
**{raid_elder}Novus Raid Academy**
Learn a raid, sharpen your mechanics, or apply to become a Raid Elder in {raid_academy_info}.
⠀
**{paragon}PvM Diary**
Check the requirements in {info_and_apply} and submit your diary in {make_a_ticket}.
⠀
**{pb1}PB Leaderboard**
See clan records in {pb_leaderboard} and submit your own with \x60/pbsubmit\x60.
⠀
**Raid Resources**
{tob_guide} • {toa_guide} • {tfa_guide}
⠀
### PvM is split by default
Any FFA arrangement, including for Ironmen, must be agreed to by **every team member before the activity starts**.
-# The player requesting FFA is responsible for keeping proof of the agreement.
⠀
Don’t be shy about asking questions or jumping into a group. See you on 346!"""


def welcome_text(guild):
    channel_names = (
        "assign-roles", "create-lfg", "raid-academy-info", "info-and-apply",
        "make-a-ticket", "pb-leaderboard", "tob-guide", "toa-guide", "tfa-guide",
    )
    tokens = {}
    guide_ids = {"tob-guide": 1545859377586511964, "toa-guide": 1548732915196764261, "tfa-guide": 1562300923823263764}
    for name in channel_names:
        if name in guide_ids:
            tokens[name.replace("-", "_")] = f"<#{guide_ids[name]}>"
            continue
        matches = [c for c in guild.text_channels if c.name == name]
        tokens[name.replace("-", "_")] = matches[0].mention if len(matches) == 1 else "#" + name
        if len(matches) != 1:
            logger.warning("Welcome template: expected one channel named %s; found %s", name, len(matches))
    emoji_aliases = {
        "raid_elder": {"raidelder", "raid_elder"},
        "paragon": {"paragon"},
        "pb1": {"pb1", "novuspb1"},
    }
    for key, aliases in emoji_aliases.items():
        matches = [emoji for emoji in guild.emojis if emoji.name.casefold() in aliases and emoji.available]
        exact = [emoji for emoji in matches if emoji.name.casefold() == key]
        chosen = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
        tokens[key] = f"{chosen} " if chosen else ""
    return WELCOME_TEXT.format(**tokens)


def is_staff(member):
    return any(role.id in STAFF for role in getattr(member, "roles", ()))

def primary_name(nickname):
    value = re.split(r"[|/]", nickname or "")[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9 _-]{1,12}", value) or not value.strip(" _-"):
        return None
    return sanitize_rsn(value)

def extract(data):
    snapshot = data.get("latestSnapshot") or {}
    bosses = (snapshot.get("data") or {}).get("bosses") or {}
    values = {}
    for _, key in RAIDS + EXTRAS + TRIAL_REFERENCE:
        metric = bosses.get(key) or {}
        kc = metric.get("kills")
        values[key] = kc if type(kc) is int and kc >= 0 else None
    for _, key, _, _ in RPP_KC:
        kc = (bosses.get(key) or {}).get("kills")
        values[key] = kc if type(kc) is int and kc >= 0 else None
    return {"values": values, "snapshot": snapshot.get("createdAt"), "name": data.get("displayName") or data.get("username")}

def trial_vouch_records(applicant_id):
    """Use /vouches records, excluding known previous trial attempts."""
    rows = get_vouches(GUILD, applicant_id)
    with closing(db()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trial_analytics_episodes'").fetchone():
            return rows, True
        episodes = conn.execute("SELECT started_at, entry_source, ended_at FROM trial_analytics_episodes WHERE guild_id=? AND discord_id=? ORDER BY episode_id DESC", (GUILD, applicant_id)).fetchall()
    if not episodes:
        return rows, True
    started, source, ended = episodes[0]
    if ended:
        return [], True
    if len(episodes) == 1 and source == "late_observation":
        return rows, True
    cutoff = datetime.datetime.fromisoformat(started)
    current = []
    for record in rows:
        try:
            stamp = datetime.datetime.fromisoformat(record[3].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=datetime.timezone.utc)
            if stamp >= cutoff:
                current.append(record)
        except (TypeError, ValueError):
            continue
    return current, False


def applicant_ca_context(values, evidence):
    """Advisory only. CA points do not satisfy Novus membership thresholds."""
    evidence=evidence or {}
    status=evidence.get("status")
    progress=rpp_progress(values,evidence.get("tasks") if status=="verified" else None)
    met=sum(stage["status"]=="baseline_met" for stage in progress["kc"])
    ranges=sum(stage["status"]=="baseline_range_review" for stage in progress["kc"])
    known=sum(stage["status"]!="unverified" for stage in progress["kc"])
    if status=="verified":
        tier=evidence.get("tier") or "Below Easy"
        count=evidence["completed_tasks"]
        ca=f"**{tier} CA** • {evidence['points']:,} points • {count} tasks"
        ca+=f"\n**RPP CAs:** {progress['ca_completed']}/{progress['ca_total']} complete"
        unknown=sum(t["status"] not in ("complete","incomplete") for t in progress["ca"])
        if unknown:
            ca+=f" • {unknown} unverified"
        if evidence.get("updated"):
            ca+=f"\n-# RuneProfile last update: {evidence['updated']}"
    else:
        ca="▫️ RuneProfile profile not found" if status=="not_found" else "▫️ RuneProfile CA data unverified"
        ca+="\n**RPP CAs:** Not verified"
    ca+=f"\n**RPP KC:** {met}/{len(progress['kc'])} baselines met"
    if ranges:
        ca+=f" • {ranges} range review"
    if known<len(progress["kc"]):
        ca+=f" • {len(progress['kc'])-known} unverified"
    raid_known=all(type(values.get(k)) is int and values[k]>=0 for _,k in RAIDS)
    cape=any(type(values.get(k)) is int and values[k]>0 for _,k in EXTRAS)
    cape_known=cape or all(type(values.get(k)) is int and values[k]>=0 for _,k in EXTRAS)
    raid_total=sum(values.get(k) or 0 for _,k in RAIDS)
    target=100 if cape else 200
    remaining=max(0,target-raid_total)
    kc_remaining=sum(max(0,s["minimum"]-s["kc"]) for s in progress["kc"] if s["kc"] is not None)
    ca_remaining=progress["ca_total"]-progress["ca_completed"]
    if raid_known and cape_known:
        comparison=f"**KC:** {remaining} raid KC remaining ({target} target)."
    else:
        comparison="**KC:** WOM data incomplete; refresh before comparing remaining KC."
    comparison+=f"\n**RPP:** {kc_remaining}{'+' if known<len(progress['kc']) else ''} additional baseline KC"
    if status=="verified":
        comparison+=f"; {ca_remaining} CAs not yet confirmed complete."
    else:
        comparison+="; CA progress unverified."
    if ranges:
        comparison+=" CG range needs staff review."
    # Advisory heuristics, not new admission requirements or automated assignments.
    if raid_known and cape_known and remaining<=20:
        guide="**Suggested: KC pathway** • close to the existing requirement."
    elif status=="verified" and progress["ca_completed"]==progress["ca_total"] and met+ranges==len(progress["kc"]):
        guide="**Suggested: RPP** • review remaining mechanical proof and staff approval."
    elif status=="verified" and evidence.get("tier") in ("Elite","Master","Grandmaster"):
        guide="**Consider Raid Trial** • strong overall CA evidence; staff must assess raid readiness and require three vouches."
        if progress["ca_completed"]>=6 and met+ranges>=6:
            guide="**Consider RPP or Raid Trial** • substantial RPP progress and strong CA evidence."
    elif status=="verified" and known==len(progress["kc"]) and (progress["ca_completed"]>=4 or met+ranges>=5) and raid_known and cape_known and kc_remaining<remaining:
        guide="**Consider RPP** • existing progress may make this more practical than the remaining raid KC."
    else:
        guide="**Staff discussion:** compare KC and RPP workload, skills and preferred pathway."
    guide=comparison+"\n"+guide
    return ca,guide

def report_embed(rsn, payload, test=False, note=None, reviewer_id=None, trial=None, logo_url=None, applicant_id=None):
    payload = payload or {}
    values = payload.get("values", {})
    total = sum(values.get(key) or 0 for _, key in RAIDS)
    incomplete = any(values.get(key) is None for _, key in RAIDS)
    special = any((values.get(key) or 0) >= 1 for _, key in EXTRAS)
    special_unknown = any(values.get(key) is None for _, key in EXTRAS)
    standard = total >= 200
    alternative = total >= 100 and special
    target = 100 if special and not standard else 200
    qualified = standard or alternative
    uncertain = not qualified and (incomplete or (total >= 100 and special_unknown))
    total_indicator = f"{check()} " if qualified else ("" if uncertain else "✖️ ")
    vouch_ready = trial is not None and trial["count"] is not None and trial["count"] >= REQUIRED_VOUCHES
    if standard or alternative:
        pathway = "200+ raid KC" if standard else "100+ raid KC + Inferno/Colosseum"
        result = f"{check()} **KC requirement met** ({pathway})"
        color = discord.Color.green()
    elif uncertain:
        result = "**KC assessment incomplete**"
        color = discord.Color.gold()
    else:
        result = "✖️ **KC thresholds not met**"
        color = discord.Color.gold()

    if vouch_ready and not qualified:
        result = f"{check()} **Raid Trial vouch requirement met**"
        color = discord.Color.green()
    elif trial is not None and not qualified:
        color = discord.Color.gold()

    def fmt(key):
        value = values.get(key)
        return f"{value:,}" if value is not None else "N/A"

    name = discord.utils.escape_markdown(payload.get("name") or rsn)
    total_label = f"{'≥ ' if incomplete else ''}{total:,} / {target:,} KC"
    embed = discord.Embed(
        title=REPORT_TITLE,
        description=(f"**Applicant:** <@{applicant_id}>\n**RSN:** {name}" if applicant_id else f"**Applicant:** {name}") + ("\n**Current role:** Trial Member • Full membership application" if trial is not None else "") + f"\n\n**Total Raid KC:** {total_indicator}**{total_label}**",
        color=color,
    )
    if logo_url:
        embed.set_thumbnail(url=logo_url)
    # Monospace rows retain their alignment on both mobile and desktop.
    rows = "\n".join(f"{label:<11} {fmt(key):>7}" for label, key in RAIDS)
    embed.add_field(name="Raid KC", value=f"\x60\x60\x60\n{rows}\n\x60\x60\x60", inline=False)
    solo_rows = "\n".join(
        f"**{label}:** {fmt(key)}" + (" KC" if values.get(key) is not None else "")
        for label, key in EXTRAS
    )
    embed.add_field(name="\u200b", value=solo_rows + "\n\u200b", inline=False)
    embed.add_field(
        name="Application Status",
        value=f"{result}\n{gear_status(reviewer_id)}\n▫️ Staff approval pending\n\u200b",
        inline=False,
    )
    if trial is not None:
        def pathway_status(passed, unknown=False):
            return f"{check()}" if passed else ("Pending verification" if unknown else "✖️")
        count = trial["count"]
        vouches = "Unavailable • staff review" if count is None else f"{(check()+' ') if vouch_ready else ''}{count}/{REQUIRED_VOUCHES} unique vouches"
        graduation = (
            f"**Raid Trial:** {vouches}\n"
            f"**200+ total raid KC:** {pathway_status(standard, incomplete)}\n"
            f"**100+ KC + Inferno/Colosseum:** {pathway_status(alternative, incomplete or special_unknown)}\n"
            "**Raids Prep Program:** Staff confirmation required"
        )
        if trial["legacy"]:
            graduation += "\n-# Existing vouch records: staff must confirm they apply to this trial."
        embed.add_field(name="Trial Member Graduation", value=graduation, inline=False)
    if not qualified:
        ca_text,guide=applicant_ca_context(values,payload.get("runeprofile"))
        embed.add_field(name="Combat Achievements & RPP",value=ca_text,inline=False)
        embed.add_field(name="Pathway Guidance",value=guide+"\n-# CA data is supporting evidence only; no automatic approval or CA-based exemption.",inline=False)
    if not qualified and trial is None:
        embed.add_field(name="Alternative Pathways", value="Raids Prep Program or Raid Trial (staff discretion).\nKC / Raids Prep Program trials: 1 month.\nRaid Trial: 3 member vouches within 2 weeks.\nBoth start when the Trial Member role is assigned.\n\u200b", inline=False)
        trial_rows = "\n".join(
            f"**{label}:** {fmt(key)}" + (" KC" if values.get(key) is not None else "")
            for label, key in TRIAL_REFERENCE
        )
        embed.add_field(name="Raid Trial Reference", value=trial_rows, inline=False)
    timestamp = payload.get("snapshot")
    if timestamp:
        try:
            timestamp = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            timestamp = timestamp.astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %I:%M %p ET").replace(", 0", ", ")
        except (ValueError, AttributeError):
            timestamp = "Unknown"
    else:
        timestamp = "Unavailable"
    footer = f"Novus Membership • WOM: {timestamp}"
    if test:
        footer += " • Staff test"
    if incomplete or special_unknown:
        embed.add_field(name="Data Note", value="N/A = unranked or unavailable; totals may be incomplete.", inline=False)
    if note:
        embed.add_field(name="Snapshot Status", value=note, inline=False)
    embed.set_footer(text=footer)
    return embed

APPLICATION_OPTIONS = {
    "standard": "200+ total raid KC",
    "alternative": "100+ raid KC + Infernal Cape / Dizana's Quiver",
    "rpp": "Raids Prep Program",
    "raidtrial": "Raid Trial (staff discretion)",
    "unsure": "Unsure / just short of requirements",
}

class ApplicationModal(discord.ui.Modal, title="✦ Novus Membership Application"):
    def __init__(self, cog, report_id, actor_id):
        super().__init__(timeout=900)
        self.cog = cog
        self.report_id = report_id
        self.actor_id = actor_id
        self.account_type = discord.ui.Select(
            placeholder="Select account type",
            options=[discord.SelectOption(label="Main", value="Main"), discord.SelectOption(label="Ironman", value="Ironman")],
            required=True,
        )
        self.pathway = discord.ui.Select(
            placeholder="Select membership option",
            options=[discord.SelectOption(label=label, value=value) for value, label in APPLICATION_OPTIONS.items()],
            required=True,
        )
        self.referral = discord.ui.TextInput(placeholder="Friend, Reddit, Discord, in-game, etc.", max_length=200, required=True)
        self.gear = discord.ui.FileUpload(required=True, min_values=1, max_values=4)
        self.details = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=500, required=False, placeholder="Anything else staff should know?")
        self.add_item(discord.ui.Label(text="Account Type", component=self.account_type))
        self.add_item(discord.ui.Label(text="Membership Pathway", component=self.pathway))
        self.add_item(discord.ui.Label(text="How did you find Novus?", component=self.referral))
        self.add_item(discord.ui.Label(text="Gear Screenshots", component=self.gear, description="Upload 1-4 images of your gear."))
        self.add_item(discord.ui.Label(text="Additional Information", component=self.details))

    async def on_submit(self, interaction):
        if interaction.user.id != self.actor_id:
            await notice(interaction, False, "This application form belongs to another user.", ephemeral=True)
            return
        await self.cog.submit_application(interaction, self.report_id, self.account_type.values[0], self.pathway.values[0], str(self.referral.value), self.gear.values, str(self.details.value or ""))

class ResubmitApplicationModal(discord.ui.Modal, title="Resubmit Application"):
    def __init__(self, cog, report_id, actor_id):
        super().__init__(timeout=900)
        self.cog, self.report_id, self.actor_id = cog, report_id, actor_id
        self.account_type = discord.ui.Select(placeholder="Keep existing account type", options=[discord.SelectOption(label=x, value=x) for x in ("Main", "Ironman")], required=False, min_values=0, max_values=1)
        self.pathway = discord.ui.Select(placeholder="Keep existing pathway", options=[discord.SelectOption(label=v, value=k) for k,v in APPLICATION_OPTIONS.items()], required=False, min_values=0, max_values=1)
        self.referral = discord.ui.TextInput(required=False, max_length=200, placeholder="Leave blank to keep existing answer")
        self.gear = discord.ui.FileUpload(required=False, min_values=0, max_values=4)
        self.details = discord.ui.TextInput(required=False, style=discord.TextStyle.paragraph, max_length=500, placeholder="Leave blank to keep existing answer")
        for label, component in (("Account Type", self.account_type), ("Membership Pathway", self.pathway), ("How did you find Novus?", self.referral), ("Replacement Gear Screenshots", self.gear), ("Additional Information", self.details)):
            self.add_item(discord.ui.Label(text=label, component=component))

    async def on_submit(self, interaction):
        if interaction.user.id != self.actor_id:
            return await notice(interaction, False, "This form belongs to another user.", ephemeral=True)
        await self.cog.resubmit_application(interaction, self.report_id, next(iter(self.account_type.values), None), next(iter(self.pathway.values), None), str(self.referral.value or "").strip(), self.gear.values, str(self.details.value or "").strip())


class ApplicationPromptView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Complete Application", style=discord.ButtonStyle.primary, custom_id="novus:application:prompt:v2")
    async def complete_application(self, interaction, button):
        await self.cog.open_application_form(interaction)

class ApplicationView(discord.ui.View):
    def __init__(self, cog, rsn=None, verified=False, approved=False, submitted=False, separate_prompt=False, trial=False):
        super().__init__(timeout=None)
        self.cog = cog
        self.remove_item(self.verify_gear if verified else self.undo_gear)
        if not trial or approved:
            self.remove_item(self.view_vouches)
        if submitted or approved or separate_prompt:
            self.remove_item(self.complete_application)
        if not submitted or approved:
            self.remove_item(self.resubmit)
        if not verified or approved:
            self.remove_item(self.final_approve)
        if approved:
            self.remove_item(self.refresh)
            self.remove_item(self.undo_gear if verified else self.verify_gear)
        if rsn:
            self.add_item(discord.ui.Button(label="View WOM Profile", url="https://wiseoldman.net/players/" + quote(rsn, safe=""), row=0))

    @discord.ui.button(label="Complete Application", style=discord.ButtonStyle.primary, custom_id="novus:application:form:v1", row=2)
    async def complete_application(self, interaction, button):
        await self.cog.open_application_form(interaction)

    @discord.ui.button(label="Resubmit Application", style=discord.ButtonStyle.secondary, custom_id="novus:application:resubmit:v1", row=2)
    async def resubmit(self, interaction, button):
        await self.cog.open_resubmit_form(interaction)

    @discord.ui.button(label="Refresh Stats", style=discord.ButtonStyle.primary, custom_id="novus:application:refresh:v1", row=0)
    async def refresh(self, interaction, button):
        await self.cog.refresh(interaction)

    @discord.ui.button(label="Verify Gear", style=discord.ButtonStyle.success, custom_id="novus:application:gear:verify:v1", row=1)
    async def verify_gear(self, interaction, button):
        await self.cog.review_gear(interaction, True)

    @discord.ui.button(label="Undo Gear Verification", style=discord.ButtonStyle.secondary, custom_id="novus:application:gear:undo:v1", row=1)
    async def undo_gear(self, interaction, button):
        await self.cog.review_gear(interaction, False)

    @discord.ui.button(label="View Vouches", style=discord.ButtonStyle.secondary, custom_id="novus:application:vouches:v1", row=2)
    async def view_vouches(self, interaction, button):
        await self.cog.show_trial_vouches(interaction)

    @discord.ui.button(label="Approve Applicant", style=discord.ButtonStyle.success, custom_id="novus:application:approve:v1", row=1)
    async def final_approve(self, interaction, button):
        await self.cog.confirm_approval(interaction)

class ApprovalChoice(discord.ui.View):
    def __init__(self, cog, actor_id, message_id):
        super().__init__(timeout=120)
        self.cog = cog
        self.actor_id = actor_id
        self.message_id = message_id

    @discord.ui.select(placeholder="Confirm completed membership pathway", options=[
        discord.SelectOption(label="200+ raid KC", value="standard"),
        discord.SelectOption(label="100+ KC + Inferno/Colosseum", value="alternative"),
        discord.SelectOption(label="Raids Prep Program completed", value="rpp"),
        discord.SelectOption(label="Raid Trial completed (3 vouches)", value="raidtrial"),
    ])
    async def choose(self, interaction, select):
        if interaction.user.id != self.actor_id:
            await notice(interaction, False, "Only the initiating staff member may confirm.", ephemeral=True)
            return
        await self.cog.complete_approval(interaction, self.message_id, select.values[0])

class ApplicationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks = {}
        self.cooldowns = {}
        self.test_checked = False
        self.recovery_loop.start()

    def cog_unload(self):
        self.recovery_loop.cancel()

    async def fetch_stats(self, rsn, update=False):
        url = "https://api.wiseoldman.net/v2/players/" + quote(rsn, safe="")
        headers = {"User-Agent": "NovusRoleSync/1.0"}
        if WOM_API_KEY:
            headers["x-api-key"] = WOM_API_KEY
        async def request(method):
            async with self.bot.http_session.request(method, url, headers=headers, timeout=20) as response:
                if response.status != 200 and response.status != 201:
                    return None, response.status
                return await response.json(), response.status
        if update:
            try:
                data, status = await request("POST")
                if data and data.get("latestSnapshot"):
                    return extract(data), None
            except Exception:
                logger.warning("WOM update request failed for application")
        data, status = await request("GET")
        if not data and status == 404 and not update:
            data, status = await request("POST")
        if not data or not data.get("latestSnapshot"):
            return None, f"WOM snapshot unavailable (HTTP {status}); request a refresh or review manually."
        return extract(data), ("Showing latest saved WOM snapshot; fresh update was not confirmed." if update else None)

    async def cache_verified(self, guild_id, discord_id, nickname):
        rsn = primary_name(nickname)
        if guild_id != GUILD or not rsn:
            return
        with closing(db()) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO application_kc_cache VALUES (?, ?, ?, NULL)", (guild_id, discord_id, rsn))
        try:
            payload, _ = await self.fetch_stats(rsn)
            if payload:
                with closing(db()) as conn, conn:
                    conn.execute("UPDATE application_kc_cache SET payload=? WHERE guild_id=? AND discord_id=? AND rsn=?", (json.dumps(payload), guild_id, discord_id, rsn))
        except Exception:
            logger.exception("Application KC verification lookup failed")

    async def resolve_applicant(self, channel):
        candidates = []
        for target, overwrite in channel.overwrites.items():
            if overwrite.view_channel is not True:
                continue
            if isinstance(target, discord.Member):
                member = target
            elif isinstance(target, discord.Object) and getattr(target, "type", None) is discord.Member:
                try:
                    member = await channel.guild.fetch_member(target.id)
                except discord.HTTPException:
                    # Do not silently discard an unresolved member overwrite.
                    return None, None
            else:
                continue
            if not member.bot:
                candidates.append(member)
        if len(candidates) != 1:
            return None, None
        member = candidates[0]
        try:
            member = await channel.guild.fetch_member(member.id)
        except discord.HTTPException:
            return None, None
        if not any(r.name.strip().casefold() == "verified" for r in member.roles):
            return None, None
        with closing(db()) as conn:
            saved = conn.execute("SELECT rsn FROM links WHERE guild_id=? AND discord_id=?", (channel.guild.id, member.id)).fetchone()
        rsn = primary_name(saved[0] if saved and saved[0] else member.nick)
        return member.id, rsn

    async def trial_context(self, channel, applicant_id):
        if not applicant_id or channel.id == TEST_CHANNEL:
            return None
        member = await channel.guild.fetch_member(applicant_id)
        if not is_trial(member):
            return None
        try:
            rows, legacy = trial_vouch_records(applicant_id)
            return {"count": len(rows), "legacy": legacy}
        except sqlite3.Error:
            logger.exception("Trial vouch lookup failed applicant=%s", applicant_id)
            return {"count": None, "legacy": True}

    async def show_trial_vouches(self, interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY or not is_staff(interaction.user):
            await notice(interaction, False, "Only staff may view trial vouches.", ephemeral=True)
            return
        row = get_report(channel.id)
        if not row or not row[0] or not interaction.message or row[2] != interaction.message.id:
            await notice(interaction, False, "This report is no longer active.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            member = await channel.guild.fetch_member(row[0])
            if not is_trial(member):
                await notice(interaction, True, "This applicant is not a current Trial Member.", ephemeral=True)
                return
            rows, legacy = trial_vouch_records(member.id)
            embed = discord.Embed(title="✦ Novus Trial Vouches", description=f"**Trial Member:** {member.mention}", color=discord.Color.green() if len(rows)>=REQUIRED_VOUCHES else discord.Color.blurple())
            embed.add_field(name="Vouch Progress", value=f"**{len(rows)}/{REQUIRED_VOUCHES}** unique vouches", inline=False)
            add_vouch_feedback(embed, rows)
            embed.set_footer(text="Novus Membership • Private staff review")
            embed.set_thumbnail(url=(channel.guild.icon or self.bot.user.display_avatar).url)
            if legacy:
                embed.add_field(name="Staff review", value="Existing vouch records may predate tracking. Confirm these apply to the current trial.", inline=False)
            await notice(interaction, True, embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception("Application vouch review failed")
            await notice(interaction, True, "Vouches could not be retrieved. Try /vouches or refresh later.", ephemeral=True)

    async def application_ca_evidence(self,rsn,payload):
        """Use the shared rate-limited client, never delay approval on missing CA data."""
        result=dict(payload)
        values=result.get("values",{})
        total=sum(values.get(k) or 0 for _,k in RAIDS)
        if total>=200 or (total>=100 and any((values.get(k) or 0)>0 for _,k in EXTRAS)):
            return result
        rp=self.bot.get_cog("RuneProfileCog")
        name=primary_name(result.get("name"))
        if not rp or not name or sanitize_rsn(name).casefold()!=sanitize_rsn(rsn).casefold():
            result["runeprofile"]={"status":"unverified"}
            return result
        async def lookup():
            profile,status=await rp.request(name)
            if status==404:
                return {"status":"not_found"}
            if status!=200 or not isinstance(profile,dict) or sanitize_rsn(str(profile.get("username",""))).casefold()!=sanitize_rsn(name).casefold():
                return {"status":"unverified"}
            raw=await rp.task_data(name)
            if not raw:
                return {"status":"unverified"}
            from cogs.runeprofile_cog import parse_tasks
            parsed=parse_tasks(raw)
            if not parsed:
                return {"status":"unverified"}
            tier,points=parsed
            updated=profile.get("updatedAt")
            if isinstance(updated,str):
                try:
                    updated=datetime.datetime.fromisoformat(updated.replace("Z","+00:00")).date().isoformat()
                except ValueError:
                    updated=None
            else:
                updated=None
            return {"status":"verified","tier":tier,"points":points,"updated":updated,
                    "completed_tasks":sum(1 for t in raw["data"] if isinstance(t,dict) and t.get("completed") is True and t.get("exempt") is False),
                    "tasks":raw}
        try:
            result["runeprofile"]=await asyncio.wait_for(lookup(),timeout=20)
        except Exception:
            logger.info("Application RuneProfile lookup unavailable for rsn=%s",name)
            result["runeprofile"]={"status":"unverified"}
        return result

    async def get_payload(self, rsn, applicant_id, update=False, include_ca=True):
        try:
            payload, note = await self.fetch_stats(rsn, update=update)
        except Exception:
            logger.exception("Application WOM lookup failed")
            payload, note = None, "WOM could not be reached. Staff review needed."
        if payload:
            if applicant_id:
                with closing(db()) as conn, conn:
                    conn.execute("INSERT OR REPLACE INTO application_kc_cache VALUES (?, ?, ?, ?)", (GUILD, applicant_id, rsn, json.dumps(payload)))
            return (await self.application_ca_evidence(rsn,payload) if include_ca else payload), note
        if applicant_id:
            with closing(db()) as conn:
                cached = conn.execute("SELECT rsn, payload FROM application_kc_cache WHERE guild_id=? AND discord_id=?", (GUILD, applicant_id)).fetchone()
            if cached and cached[1] and sanitize_rsn(cached[0]).casefold() == sanitize_rsn(rsn).casefold():
                payload=json.loads(cached[1])
                return (await self.application_ca_evidence(rsn,payload) if include_ca else payload), "WOM lookup failed; showing previously saved snapshot. Staff should refresh before approval."
        return None, note

    async def ensure_form_prompt(self, channel, submitted=False):
        prompt_id = get_form_prompt(channel.id)
        if prompt_id:
            try:
                prompt = await channel.fetch_message(prompt_id)
                if prompt.author.id == self.bot.user.id:
                    return prompt
                raise RuntimeError("Saved application prompt is not owned by Novus Sync")
            except discord.NotFound:
                pass
        # Recover a standalone prompt if posting succeeded immediately before a restart.
        async for candidate in channel.history(limit=30):
            if candidate.author.id == self.bot.user.id and not candidate.embeds and candidate.content == APPLICATION_INTRO and any(
                getattr(child, "custom_id", None) == "novus:application:prompt:v2"
                for action_row in candidate.components for child in action_row.children
            ):
                save_form_prompt(channel.id, candidate.id)
                return candidate
        prompt = await channel.send(
            APPLICATION_SUBMITTED if submitted else APPLICATION_INTRO,
            view=None if submitted else ApplicationPromptView(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        save_form_prompt(channel.id, prompt.id)
        return prompt

    async def update_form_prompt(self, channel, content):
        prompt_id = get_form_prompt(channel.id)
        if prompt_id:
            try:
                prompt = await channel.fetch_message(prompt_id)
            except discord.NotFound:
                return
            if prompt.author.id == self.bot.user.id:
                await prompt.edit(content=content, view=None, allowed_mentions=discord.AllowedMentions.none())

    async def separate_test_report(self, channel, row):
        # Only migrate the explicitly designated test ticket; preserve its saved reviews.
        if channel.id != TEST_CHANNEL or final_approval(channel.id, row[2]):
            return
        applicant_id, rsn, old_id = row
        old = await channel.fetch_message(old_id)
        if old.author.id != self.bot.user.id or not old.embeds or old.embeds[0].title not in REPORT_TITLES:
            return
        prompt_id = get_form_prompt(channel.id)
        if prompt_id and prompt_id < old_id and not old.content:
            return
        submitted = bool(form_submission(channel.id, old_id))
        reviewer = gear_reviewer(channel.id, old_id)
        prompt = await self.ensure_form_prompt(channel, submitted)
        # The old report stays intact until the replacement and database migration succeed.
        replacement = await channel.send(embed=old.embeds[0].copy(), view=ApplicationView(self, rsn, verified=bool(reviewer), submitted=submitted, separate_prompt=True), allowed_mentions=discord.AllowedMentions.none())
        try:
            with closing(db()) as conn, conn:
                conn.execute("UPDATE application_kc_reports SET message_id=? WHERE channel_id=? AND message_id=?", (replacement.id, channel.id, old_id))
                conn.execute("UPDATE application_gear_reviews SET message_id=? WHERE channel_id=? AND message_id=?", (replacement.id, channel.id, old_id))
                conn.execute("UPDATE application_form_submissions SET report_id=? WHERE channel_id=? AND report_id=?", (replacement.id, channel.id, old_id))
        except Exception:
            await replacement.delete()
            raise
        try:
            await old.delete()
        except discord.HTTPException:
            logger.warning("Replacement test report active; old report could not be removed")
        logger.info("Application prompt separated in test ticket channel=%s prompt=%s report=%s", channel.id, prompt.id, replacement.id)

    async def publish(self, channel):
        if channel.guild.id != GUILD or channel.category_id != CATEGORY:
            return
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            if get_report(channel.id):
                return
            test = channel.id == TEST_CHANNEL
            if test:
                applicant_id, rsn = None, "Vecs"
            else:
                applicant_id, rsn = await self.resolve_applicant(channel)
            if not rsn:
                logger.warning("Application applicant not unambiguous in channel %s; skipping report", channel.id)
                return
            # Recover a bot-owned report if posting succeeded before the database write.
            async for msg in channel.history(limit=100):
                if msg.author.id != self.bot.user.id or not msg.embeds or msg.embeds[0].title not in REPORT_TITLES:
                    continue
                description = msg.embeds[0].description or ""
                expected = "**Applicant:** " + discord.utils.escape_markdown(rsn)
                if description.split("\n", 1)[0].casefold() != expected.casefold() and not (applicant_id and description.split("\n")[:2] == [f"**Applicant:** <@{applicant_id}>", "**RSN:** " + discord.utils.escape_markdown(rsn)]):
                    logger.warning("Unmapped application report has mismatched RSN in %s; manual review needed", channel.id)
                    return
                save_report(channel.id, applicant_id, rsn, msg.id)
                logger.info("Recovered application report channel=%s message=%s", channel.id, msg.id)
                return
            # Capture the applicant's status before any approval can change roles.
            if applicant_id and not test:
                try:
                    member = await channel.guild.fetch_member(applicant_id)
                    role_names = {role.name.strip().casefold() for role in member.roles}
                    origin = "existing" if "member" in role_names else ("trial" if "trial member" in role_names else "new")
                    with closing(db()) as conn, conn:
                        conn.execute("CREATE TABLE IF NOT EXISTS council_application_origins (channel_id INTEGER PRIMARY KEY, applicant_id INTEGER, origin TEXT NOT NULL, recorded_at TEXT NOT NULL)")
                        conn.execute("INSERT OR IGNORE INTO council_application_origins VALUES (?,?,?,?)", (channel.id, applicant_id, origin, datetime.datetime.now(datetime.timezone.utc).isoformat()))
                except Exception:
                    logger.exception("Council application origin not recorded; application continues")
            payload, note = await self.get_payload(rsn, applicant_id, update=True)
            trial = await self.trial_context(channel, applicant_id)
            await self.ensure_form_prompt(channel)
            message = await channel.send(embed=report_embed(rsn, payload, test, note, trial=trial, logo_url=(channel.guild.icon or self.bot.user.display_avatar).url, applicant_id=applicant_id), view=ApplicationView(self, rsn, separate_prompt=True, trial=trial is not None), allowed_mentions=discord.AllowedMentions.none())
            save_report(channel.id, applicant_id, rsn, message.id)
            logger.info("Application KC report published channel=%s message=%s rsn=%s total=%s", channel.id, message.id, rsn, sum((payload or {}).get("values", {}).get(k) or 0 for _,k in RAIDS))

    @commands.Cog.listener()
    async def on_ready(self):
        if self.test_checked:
            return
        self.test_checked = True
        channel = self.bot.get_channel(TEST_CHANNEL)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(TEST_CHANNEL)
            except discord.HTTPException:
                logger.warning("Application test ticket unavailable")
                return
        try:
            if isinstance(channel, discord.TextChannel) and channel.guild.id == GUILD and channel.category_id == CATEGORY:
                row = get_report(channel.id)
                if row:
                    await self.separate_test_report(channel, row)
                    row = get_report(channel.id)
                    applicant_id, rsn, message_id = row
                    message = await channel.fetch_message(message_id)
                    if message.author.id == self.bot.user.id and any(e.title in REPORT_TITLES for e in message.embeds) and not final_approval(channel.id, message.id):
                        payload, note = await self.get_payload(rsn, applicant_id, update=False)
                        if payload:
                            reviewer = gear_reviewer(channel.id, message.id)
                            await message.edit(content=form_content(channel.id, message.id), embed=report_embed(rsn, payload, True, note, reviewer, logo_url=(channel.guild.icon or self.bot.user.display_avatar).url, applicant_id=applicant_id), view=ApplicationView(self, rsn, verified=bool(reviewer), submitted=bool(form_submission(channel.id, message.id)), separate_prompt=bool(get_form_prompt(channel.id))), allowed_mentions=discord.AllowedMentions.none())
                            logger.info("Restyled existing application test report channel=%s message=%s", channel.id, message.id)
                else:
                    await self.publish(channel)
                if not get_form_prompt(channel.id):
                    await self.test_application_messages(channel)
        except Exception:
            logger.exception("Application test report failed")

    async def test_application_messages(self, channel):
        # One-time, explicitly requested rendering test; never invokes approval or changes roles.
        if channel.guild.id != GUILD or channel.category_id != CATEGORY or channel.id != TEST_CHANNEL:
            return
        key = "vecs-intro-welcome-20261125-v2"
        marker = "**Staff test - welcome message preview (no roles changed)**\n"
        with closing(db()) as conn:
            if conn.execute("SELECT 1 FROM application_message_tests WHERE test_key=?", (key,)).fetchone():
                return
        row = get_report(channel.id)
        if not row:
            raise RuntimeError("Test report not found")
        report = await channel.fetch_message(row[2])
        if report.author.id != self.bot.user.id or not any(e.title in REPORT_TITLES for e in report.embeds):
            raise RuntimeError("Test report ownership or title mismatch")
        if report.content != APPLICATION_INTRO:
            if final_approval(channel.id, report.id):
                intro_marker = "**Staff test - application introduction preview**\n"
                async for candidate in channel.history(limit=50):
                    if candidate.author.id == self.bot.user.id and candidate.content == intro_marker + APPLICATION_INTRO:
                        break
                else:
                    await channel.send(intro_marker + APPLICATION_INTRO, allowed_mentions=discord.AllowedMentions.none())
            else:
                await report.edit(content=APPLICATION_INTRO, allowed_mentions=discord.AllowedMentions.none())
                report = await channel.fetch_message(report.id)
                if report.content != APPLICATION_INTRO:
                    raise RuntimeError("Introduction content mismatch")
        logger.info("Application message test: intro verified channel=%s report=%s", channel.id, report.id)
        expected = marker + welcome_text(channel.guild)
        preview = None
        async for candidate in channel.history(limit=50):
            if candidate.author.id == self.bot.user.id and candidate.content.startswith(marker):
                preview = candidate
                if candidate.content != expected:
                    await candidate.edit(content=expected, allowed_mentions=discord.AllowedMentions.none())
                break
        if preview is None:
            preview = await channel.send(expected, allowed_mentions=discord.AllowedMentions.none())
        verified = await channel.fetch_message(preview.id)
        if verified.content != expected:
            raise RuntimeError("Welcome preview content mismatch")
        with closing(db()) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO application_message_tests VALUES (?, ?)", (key, preview.id))
        logger.info("Application message test: welcome verified channel=%s message=%s; no roles changed", channel.id, preview.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            return
        await asyncio.sleep(10)
        try:
            fresh = await self.bot.fetch_channel(channel.id)
            await self.publish(fresh)
        except Exception:
            logger.exception("New application report failed for %s", channel.id)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if not isinstance(after, discord.TextChannel) or after.guild.id != GUILD or after.category_id != CATEGORY or get_report(after.id):
            return
        if before.category_id == after.category_id and before.overwrites == after.overwrites:
            return
        await asyncio.sleep(3)
        try:
            await self.publish(await self.bot.fetch_channel(after.id))
        except Exception:
            logger.exception("Application channel update retry failed channel=%s", after.id)

    @commands.Cog.listener()
    async def on_message(self, message):
        channel = message.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            return
        if message.author.id == self.bot.user.id or not (message.author.bot or message.webhook_id):
            return
        if not any("application created" in ((e.title or "") + "\n" + (e.description or "")).casefold() for e in message.embeds):
            return
        await asyncio.sleep(3)
        try:
            await self.publish(await self.bot.fetch_channel(channel.id))
        except Exception:
            logger.exception("Application opening-message report failed")

    def form_allowed(self, channel, actor, report_id):
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            return False
        row = get_report(channel.id)
        return bool(row and row[2] == report_id and (row[0] == actor.id or (channel.id == TEST_CHANNEL and is_staff(actor))))

    async def open_application_form(self, interaction):
        channel = interaction.channel
        report_id = interaction.message.id if interaction.message else None
        if isinstance(channel, discord.TextChannel) and report_id == get_form_prompt(channel.id):
            row = get_report(channel.id)
            if not row:
                await notice(interaction, False, "Your eligibility report is being prepared. Try again shortly.", ephemeral=True)
                return
            report_id = row[2]
        if not self.form_allowed(channel, interaction.user, report_id):
            await notice(interaction, False, "Only the applicant may complete this form.", ephemeral=True)
            return
        if final_approval(channel.id, report_id) or form_submission(channel.id, report_id):
            await notice(interaction, False, "Application already completed. Post any additional details in this ticket.", ephemeral=True)
            return
        if gear_reviewer(channel.id, report_id):
            await notice(interaction, False, "Gear is already verified. Ask staff to undo verification before submitting new gear.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal(self, report_id, interaction.user.id))

    async def open_resubmit_form(self, interaction):
        channel = interaction.channel
        report_id = interaction.message.id if interaction.message else None
        if not self.form_allowed(channel, interaction.user, report_id) or final_approval(channel.id, report_id) or not form_submission(channel.id, report_id):
            return await notice(interaction, False, "Only the applicant may revise an unapproved application.", ephemeral=True)
        await interaction.response.send_modal(ResubmitApplicationModal(self, report_id, interaction.user.id))

    async def resubmit_application(self, interaction, report_id, account_type, pathway, referral, attachments, details):
        channel = interaction.channel
        if not self.form_allowed(channel, interaction.user, report_id):
            return await notice(interaction, False, "This form is no longer available.", ephemeral=True)
        if (account_type and account_type not in ("Main", "Ironman")) or (pathway and pathway not in APPLICATION_OPTIONS):
            return await notice(interaction, False, "Invalid application selection.", ephemeral=True)
        if len(attachments) > 4 or any(not (a.content_type or "").startswith("image/") for a in attachments):
            return await notice(interaction, False, "Upload up to four image files.", ephemeral=True)
        limit = min(getattr(channel.guild, "filesize_limit", 10 * 1024 * 1024), 10 * 1024 * 1024)
        if any(a.size > limit for a in attachments) or sum(a.size for a in attachments) > 25 * 1024 * 1024:
            return await notice(interaction, False, "Screenshots are too large.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self.locks.setdefault(channel.id, asyncio.Lock()):
            submission = form_submission(channel.id, report_id)
            if not self.form_allowed(channel, interaction.user, report_id) or not submission or final_approval(channel.id, report_id):
                return await notice(interaction, True, "This application is no longer editable.", ephemeral=True)
            files = []
            try:
                original = await channel.fetch_message(submission[1])
                report = await channel.fetch_message(report_id)
                if original.author.id != self.bot.user.id or not original.embeds or original.embeds[0].title not in DETAILS_TITLES or report.author.id != self.bot.user.id or not report.embeds or report.embeds[0].title not in REPORT_TITLES:
                    return await notice(interaction, True, "Original application unavailable. Contact staff.", ephemeral=True)
                embed = original.embeds[0].copy()
                embed.title = DETAILS_TITLE
                values = {f.name: f.value for f in embed.fields}
                updates = {}
                if account_type: updates["Account Type"] = account_type
                if pathway: updates["Membership Pathway"] = APPLICATION_OPTIONS[pathway]
                if referral: updates["How They Found Us"] = discord.utils.escape_markdown(referral)
                if details: updates["Additional Information"] = discord.utils.escape_markdown(details)
                if any(not (updates.get(k) or values.get(k)) for k in ("Account Type", "Membership Pathway", "How They Found Us")):
                    return await notice(interaction, True, "Complete any missing required answers.", ephemeral=True)
                retained = [a for a in original.attachments if (a.content_type or "").startswith("image/") or re.search(r"\.(png|jpe?g|gif|webp|bmp|avif)$", a.filename, re.I)]
                if not attachments and not retained:
                    return await notice(interaction, True, "Original gear screenshots are missing. Upload replacements.", ephemeral=True)
                if not attachments and not any(values.get(k) != v for k,v in updates.items()):
                    return await notice(interaction, True, "No application changes submitted.", ephemeral=True)
                if attachments:
                    for i, attachment in enumerate(attachments, 1):
                        suffix = attachment.filename.rsplit(".", 1)[-1].lower()
                        if not re.fullmatch(r"[a-z0-9]{1,5}", suffix): suffix = "img"
                        files.append(await asyncio.wait_for(attachment.to_file(filename=f"gear-{report_id}-{i}.{suffix}"), timeout=30))
                images = files if attachments else retained
                count = len(images)
                # Any actual revision requires fresh staff review; preserve unchanged answers.
                updates["Gear Screenshots"] = f"{count} screenshot{'s' if count != 1 else ''} attached"
                updates["Application Status"] = f"{check()} **Application resubmitted**\n▫️ Gear verification pending\n▫️ Staff approval pending"
                for name, value in updates.items():
                    index = next((i for i,f in enumerate(embed.fields) if f.name == name), None)
                    if index is None: embed.add_field(name=name, value=value, inline=False)
                    else: embed.set_field_at(index, name=name, value=value, inline=False)
                embed.set_footer(text="Novus Membership • Staff review pending")
                embed.set_image(url=f"attachment://{images[0].filename}")
                embeds = [embed]
                for i, attachment in enumerate(images[1:], 2):
                    extra = discord.Embed(title=f"Gear Screenshot {i} of {count}", color=discord.Color.blurple())
                    extra.set_image(url=f"attachment://{attachment.filename}")
                    embeds.append(extra)
                # Clear approval eligibility before editing evidence, so failures fail closed.
                set_gear_review(channel.id, report_id, None)
                report_card = report.embeds[0].copy()
                report_card.title = REPORT_TITLE
                for i, field in enumerate(report_card.fields):
                    if field.name == "Application Status":
                        kc = field.value.replace("\\n", "\n").partition("\n")[0]
                        report_card.set_field_at(i, name=field.name, value=f"{kc}\n▫️ Gear verification pending\n▫️ Staff approval pending", inline=False)
                row = get_report(channel.id)
                await report.edit(embed=report_card, view=ApplicationView(self, row[1], submitted=True, separate_prompt=bool(get_form_prompt(channel.id)), trial=any(f.name == "Trial Member Graduation" for f in report_card.fields)), allowed_mentions=discord.AllowedMentions.none())
                saved = await original.edit(embeds=embeds, attachments=images, allowed_mentions=discord.AllowedMentions.none())
                confirmed = await channel.fetch_message(saved.id)
                if len(confirmed.attachments) != count:
                    visible = sum(bool(e.image.url) for e in confirmed.embeds)
                    if visible != count:
                        raise RuntimeError("Gear attachment and embedded image readback mismatch")
                    logger.warning("Gear images rendered but attachment metadata missing: channel=%s report=%s expected=%s attachments=%s", channel.id, report_id, count, len(confirmed.attachments))
                logger.info("Application resubmitted channel=%s report=%s attachments=%s", channel.id, report_id, count)
                await notice(interaction, True, "Application updated. Staff will review your revised application.", ephemeral=True)
            except Exception:
                logger.exception("Application resubmission failed channel=%s", channel.id)
                await notice(interaction, True, "Unable to finish resubmission. Check your ticket and retry or contact staff.", ephemeral=True)
            finally:
                for file in files: file.close()

    async def submit_application(self, interaction, report_id, account_type, pathway, referral, attachments, details):
        channel = interaction.channel
        if not self.form_allowed(channel, interaction.user, report_id):
            await notice(interaction, False, "This form is no longer available to you.", ephemeral=True)
            return
        referral = referral.strip()
        details = details.strip()
        if account_type not in ("Main", "Ironman") or pathway not in APPLICATION_OPTIONS or not referral or not 1 <= len(attachments) <= 4:
            await notice(interaction, False, "Please complete all required fields and include gear screenshots. Reopen the form to try again.", ephemeral=True)
            return
        if any(not (a.content_type or "").startswith("image/") for a in attachments):
            await notice(interaction, False, "Please upload image files only (PNG, JPG, or another supported image format). Reopen the form to try again.", ephemeral=True)
            return
        limit = min(getattr(channel.guild, "filesize_limit", 10 * 1024 * 1024), 10 * 1024 * 1024)
        if any(a.size > limit for a in attachments) or sum(a.size for a in attachments) > 25 * 1024 * 1024:
            await notice(interaction, False, "Screenshots are too large. Please use smaller images and reopen the form.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            if not self.form_allowed(channel, interaction.user, report_id) or final_approval(channel.id, report_id) or form_submission(channel.id, report_id) or gear_reviewer(channel.id, report_id):
                await notice(interaction, True, "Application already submitted, verified or closed. Contact staff for changes.", ephemeral=True)
                return
            files = []
            try:
                report = await channel.fetch_message(report_id)
                if report.author.id != self.bot.user.id or not any(e.title in REPORT_TITLES for e in report.embeds):
                    await notice(interaction, True, "The application report is no longer available.", ephemeral=True)
                    return
                # Recover a submission whose Discord send succeeded before the database commit.
                # The new attachment filenames carry the report ID, replacing
                # the visible reply reference while retaining restart recovery.
                marker = f"gear-{report_id}-"
                async for candidate in channel.history(limit=100, after=report, oldest_first=False):
                    if candidate.author.id != self.bot.user.id or not candidate.embeds or not candidate.attachments:
                        continue
                    legacy = candidate.embeds[0].title == "Application Details" and candidate.reference and candidate.reference.message_id == report_id
                    current = candidate.embeds[0].title in DETAILS_TITLES and any(a.filename.startswith(marker) for a in candidate.attachments)
                    if legacy or current:
                        save_form_submission(channel.id, report_id, interaction.user.id, candidate.id)
                        try:
                            await self.update_form_prompt(channel, APPLICATION_SUBMITTED)
                            prompt_id = get_form_prompt(channel.id)
                            await report.edit(content=None if prompt_id else APPLICATION_SUBMITTED, view=ApplicationView(self, get_report(channel.id)[1], submitted=True, separate_prompt=bool(prompt_id), trial=any(f.name == "Trial Member Graduation" for f in report.embeds[0].fields)), allowed_mentions=discord.AllowedMentions.none())
                        except discord.HTTPException:
                            logger.exception("Recovered submission saved but application UI update failed")
                        await notice(interaction, True, "Your existing application was recovered. Staff can review it now.", ephemeral=True)
                        return
                row = get_report(channel.id)
                rsn = row[1]
                escape = discord.utils.escape_markdown
                response = discord.Embed(title=DETAILS_TITLE, color=discord.Color.blurple())
                response.description = f"**Applicant:** {escape(rsn)}"
                logo = channel.guild.icon or self.bot.user.display_avatar
                response.set_thumbnail(url=logo.url)
                response.add_field(name="Account Type", value=account_type, inline=False)
                response.add_field(name="Membership Pathway", value=APPLICATION_OPTIONS[pathway], inline=False)
                response.add_field(name="How They Found Us", value=escape(referral), inline=False)
                if details:
                    response.add_field(name="Additional Information", value=escape(details), inline=False)
                count = len(attachments)
                response.add_field(name="Gear Screenshots", value=f"{count} screenshot{'s' if count != 1 else ''} attached", inline=False)
                response.add_field(name="Application Status", value=f"{check()} **Application submitted**\n▫️ Gear verification pending\n▫️ Staff approval pending", inline=False)
                response.set_footer(text="Novus Membership • Staff review pending")
                for i, attachment in enumerate(attachments, 1):
                    suffix = attachment.filename.rsplit(".", 1)[-1].lower()
                    if not re.fullmatch(r"[a-z0-9]{1,5}", suffix):
                        suffix = "img"
                    files.append(await asyncio.wait_for(attachment.to_file(filename=f"{marker}{i}.{suffix}"), timeout=30))
                # Reference every upload in an embed so Discord displays the
                # first within the details card and extras as separate images below.
                response.set_image(url=f"attachment://{files[0].filename}")
                embeds = [response]
                for i, file in enumerate(files[1:], 2):
                    extra = discord.Embed(title=f"Gear Screenshot {i} of {count}", color=discord.Color.blurple())
                    extra.set_image(url=f"attachment://{file.filename}")
                    embeds.append(extra)
                submitted = await channel.send(embeds=embeds, files=files, allowed_mentions=discord.AllowedMentions.none())
                save_form_submission(channel.id, report_id, interaction.user.id, submitted.id)
                try:
                    prompt_id = get_form_prompt(channel.id)
                    if prompt_id:
                        await self.update_form_prompt(channel, APPLICATION_SUBMITTED)
                    await report.edit(content=None if prompt_id else APPLICATION_SUBMITTED, view=ApplicationView(self, rsn, submitted=True, separate_prompt=bool(prompt_id), trial=any(f.name == "Trial Member Graduation" for f in report.embeds[0].fields)), allowed_mentions=discord.AllowedMentions.none())
                except discord.HTTPException:
                    logger.exception("Form saved but report button update failed")
                logger.info("Application form submitted channel=%s report=%s response=%s attachments=%s", channel.id, report_id, submitted.id, len(attachments))
                await notice(interaction, True, "Application submitted. Staff will review your details and gear.", ephemeral=True)
            except Exception:
                logger.exception("Application form submission failed channel=%s", channel.id)
                await notice(interaction, True, "Could not complete submission. Check the ticket before retrying; if screenshots already appear, ask staff to review them. You can also post details and gear manually.", ephemeral=True)
            finally:
                for file in files:
                    file.close()

    async def confirm_approval(self, interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY or not is_staff(interaction.user):
            await notice(interaction, False, "Only staff may approve applications.", ephemeral=True)
            return
        row = get_report(channel.id)
        if not row or not interaction.message or row[2] != interaction.message.id:
            await notice(interaction, False, "This report is no longer active.", ephemeral=True)
            return
        if final_approval(channel.id, row[2]) or not gear_reviewer(channel.id, row[2]):
            await notice(interaction, False, "Application already approved or gear review still pending.", ephemeral=True)
            return
        if row[0] == interaction.user.id:
            await notice(interaction, False, "Another staff member must approve your application.", ephemeral=True)
            return
        await notice(interaction, False, 
            "Select the completed membership pathway to confirm final approval. Selecting a pathway grants Member access and posts the welcome message." + ("\n**Test ticket: no roles will change.**" if channel.id == TEST_CHANNEL else ""),
            view=ApprovalChoice(self, interaction.user.id, row[2]), ephemeral=True,
        )

    async def complete_approval(self, interaction, message_id, pathway):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY or not is_staff(interaction.user) or pathway not in PATHWAYS:
            await notice(interaction, False, "Only authorized staff may approve applications.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            row = get_report(channel.id)
            if not row or row[2] != message_id or not gear_reviewer(channel.id, message_id) or final_approval(channel.id, message_id):
                await notice(interaction, True, "Approval is no longer available. Check gear verification and application status.", ephemeral=True)
                return
            applicant_id, rsn, _ = row
            test = channel.id == TEST_CHANNEL
            if applicant_id == interaction.user.id or (not applicant_id and not test):
                await notice(interaction, True, "Applicant identity cannot be confirmed for approval.", ephemeral=True)
                return
            try:
                message = await channel.fetch_message(message_id)
                if message.author.id != self.bot.user.id or not message.embeds or message.embeds[0].title not in REPORT_TITLES:
                    await notice(interaction, True, "This report is no longer active.", ephemeral=True)
                    return
                embed = message.embeds[0].copy()
                status_index = next((i for i, field in enumerate(embed.fields) if field.name == "Application Status"), None)
                next_index = next((i for i, field in enumerate(embed.fields) if field.name == "Next Step"), None)
                if status_index is None:
                    await notice(interaction, True, "Refresh the report before approval.", ephemeral=True)
                    return
                if pathway in ("standard", "alternative"):
                    payload, note = await self.get_payload(rsn, applicant_id, update=False, include_ca=False)
                    values = (payload or {}).get("values", {})
                    total = sum(values.get(key) or 0 for _, key in RAIDS)
                    special = any((values.get(key) or 0) >= 1 for _, key in EXTRAS)
                    valid = total >= 200 if pathway == "standard" else total >= 100 and special
                    if not valid:
                        await notice(interaction, True, "WOM does not confirm the selected pathway. Refresh stats or select a genuinely completed manual pathway.", ephemeral=True)
                        return
                member = None
                logged = True
                trial_episode_id = None
                if not test:
                    member = await channel.guild.fetch_member(applicant_id)
                    current_id, current_rsn = await self.resolve_applicant(await self.bot.fetch_channel(channel.id))
                    if current_id != applicant_id or not current_rsn or sanitize_rsn(current_rsn).casefold() != sanitize_rsn(rsn).casefold():
                        await notice(interaction, True, "Applicant or RSN no longer matches this report. Use manual staff review.", ephemeral=True)
                        return
                    if pathway == "raidtrial":
                        if not is_trial(member):
                            await notice(interaction, True, "The Raid Trial pathway requires a current Trial Member. Choose another completed pathway.", ephemeral=True)
                            return
                        rows, legacy = trial_vouch_records(applicant_id)
                        if len(rows) < REQUIRED_VOUCHES:
                            await notice(interaction, True, f"Only {len(rows)}/{REQUIRED_VOUCHES} vouches recorded for this trial. Wait for more vouches or select another completed pathway.", ephemeral=True)
                            return
                    verify = self.bot.get_cog("VerifyCog")
                    if verify is None or not hasattr(verify, "approve_member"):
                        await notice(interaction, True, "Approval service unavailable. No roles changed.", ephemeral=True)
                        return
                    if is_trial(member):
                        # Capture the episode before the approval removes the
                        # trial role and the analytics listener closes it.
                        with closing(db()) as conn:
                            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='trial_analytics_episodes'").fetchone():
                                current = conn.execute("SELECT episode_id FROM trial_analytics_episodes WHERE guild_id=? AND discord_id=? AND ended_at IS NULL", (GUILD, applicant_id)).fetchone()
                                trial_episode_id = current[0] if current else None
                    member, already, logged = await verify.approve_member(channel.guild, interaction.user, member, pathway=PATHWAYS[pathway])
                save_final_approval(channel.id, message_id, interaction.user.id, pathway,
                                    trial_episode_id=trial_episode_id, applicant_id=applicant_id)
                try:
                    await self.finish_approved_application(channel, message_id)
                    welcome_status = "Welcome message posted."
                except Exception:
                    logger.exception("Approval saved but final delivery pending channel=%s", channel.id)
                    welcome_status = "Approval saved. Welcome/report delivery will retry automatically; do not reapprove."
                logger.info("Application final approval channel=%s applicant=%s actor=%s pathway=%s test=%s", channel.id, applicant_id, interaction.user.id, pathway, test)
                await notice(interaction, True, ("Test approval complete; no roles changed. " if test else "Applicant approved. ") + welcome_status + ("" if logged else " Staff approval log could not be sent."), ephemeral=True)
            except ValueError as exc:
                await notice(interaction, True, str(exc), ephemeral=True)
            except Exception:
                logger.exception("Application approval failed for channel %s", channel.id)
                await notice(interaction, True, "Approval could not finish. Check the applicant's roles and report before retrying.", ephemeral=True)

    async def finish_approved_application(self, channel, report_id):
        row = get_report(channel.id)
        approval = final_approval(channel.id, report_id)
        if not row or row[2] != report_id or not approval:
            raise RuntimeError("Approval record mismatch")
        if delivered_welcome(channel.id, report_id):
            return
        applicant_id, rsn, _ = row
        reviewer_id, pathway = approval
        test = channel.id == TEST_CHANNEL
        report = await channel.fetch_message(report_id)
        if report.author.id != self.bot.user.id or not report.embeds or report.embeds[0].title not in REPORT_TITLES:
            raise RuntimeError("Approved report ownership mismatch")
        embed = report.embeds[0].copy()
        index = next((i for i, field in enumerate(embed.fields) if field.name == "Application Status"), None)
        if index is None:
            raise RuntimeError("Approved report status missing")
        kc_result = embed.fields[index].value.partition("\n")[0]
        embed.set_field_at(index, name="Application Status", value=f"{kc_result}\n{gear_status(gear_reviewer(channel.id, report_id))}\n{check()} **{'Test approval' if test else 'Staff approval'} complete** (by <@{reviewer_id}>)\n**Pathway:** {PATHWAYS[pathway]}", inline=False)
        if not test:
            embed.color = discord.Color.green()
        embed.set_thumbnail(url=(channel.guild.icon or self.bot.user.display_avatar).url)
        await report.edit(content=None, embed=embed, view=ApplicationView(self, rsn, verified=True, approved=True), allowed_mentions=discord.AllowedMentions.none())
        if get_form_prompt(channel.id):
            await self.update_form_prompt(channel, "**Application complete.**")
        # Reply reference identifies the welcome even if a restart interrupted the database write.
        async for candidate in channel.history(limit=150, after=report):
            if candidate.author.id == self.bot.user.id and candidate.reference and candidate.reference.message_id == report_id and "## Welcome to Novus!" in candidate.content:
                mark_welcome_delivered(channel.id, report_id, candidate.id)
                return
        prefix = "**Staff test - welcome preview (no roles changed)**\n" if test else f"<@{applicant_id}>\n"
        welcome = await channel.send(prefix + welcome_text(channel.guild), reference=report.to_reference(fail_if_not_exists=False), mention_author=False, allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=applicant_id)] if applicant_id and not test else False, roles=False, everyone=False))
        mark_welcome_delivered(channel.id, report_id, welcome.id)
        logger.info("Application welcome delivered channel=%s report=%s welcome=%s", channel.id, report_id, welcome.id)

    @tasks.loop(minutes=5)
    async def recovery_loop(self):
        for channel_id, report_id in pending_deliveries():
            try:
                channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
                    continue
                async with self.locks.setdefault(channel.id, asyncio.Lock()):
                    await self.finish_approved_application(channel, report_id)
            except discord.NotFound:
                # Closed/deleted tickets need no delivery.
                with closing(db()) as conn, conn:
                    conn.execute("DELETE FROM application_welcome_delivery WHERE channel_id=? AND report_id=? AND welcome_id IS NULL", (channel_id, report_id))
            except Exception:
                logger.exception("Application delivery retry failed channel=%s", channel_id)
        # Catch recently created tickets missed during reconnects or delayed permission updates.
        guild = self.bot.get_guild(GUILD)
        if not guild:
            return
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)
        for channel in guild.text_channels:
            if channel.id == TEST_CHANNEL or channel.category_id != CATEGORY or channel.created_at < cutoff or get_report(channel.id):
                continue
            try:
                # Limit recovery to tickets with a genuine bot-generated opening embed.
                found = False
                async for message in channel.history(limit=20, oldest_first=True):
                    if message.author.id != self.bot.user.id and (message.author.bot or message.webhook_id) and any("application created" in ((e.title or "") + "\n" + (e.description or "")).casefold() for e in message.embeds):
                        found = True
                        break
                if found:
                    await self.publish(await self.bot.fetch_channel(channel.id))
            except Exception:
                logger.exception("Application ticket recovery failed channel=%s", channel.id)

    @recovery_loop.before_loop
    async def before_recovery(self):
        await self.bot.wait_until_ready()

    async def review_gear(self, interaction, verified):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            await notice(interaction, False, "This action is only available in application tickets.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await notice(interaction, False, "Only staff may verify gear.", ephemeral=True)
            return
        row = get_report(channel.id)
        if not row or not interaction.message or interaction.message.id != row[2]:
            await notice(interaction, False, "This report is no longer active.", ephemeral=True)
            return
        applicant_id, rsn, message_id = row
        if applicant_id == interaction.user.id and channel.id != TEST_CHANNEL:
            await notice(interaction, False, "Another staff member must verify your gear.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            try:
                message = await channel.fetch_message(message_id)
                if message.author.id != self.bot.user.id or not message.embeds or message.embeds[0].title not in REPORT_TITLES:
                    await notice(interaction, True, "This report is no longer active.", ephemeral=True)
                    return
                embed = message.embeds[0].copy()
                status_index = next((i for i, field in enumerate(embed.fields) if field.name == "Application Status"), None)
                next_index = next((i for i, field in enumerate(embed.fields) if field.name == "Next Step"), None)
                if status_index is None:
                    await notice(interaction, True, "Refresh the report before verifying gear.", ephemeral=True)
                    return
                if final_approval(channel.id, message_id):
                    await notice(interaction, True, "This application has already been approved.", ephemeral=True)
                    return
                previous = gear_reviewer(channel.id, message_id)
                if bool(previous) == verified:
                    await notice(interaction, True, "Gear is already verified." if verified else "Gear verification is already pending.", ephemeral=True)
                    return
                reviewer = interaction.user.id if verified else None
                status = embed.fields[status_index].value
                result, _, _ = status.partition("\n")
                embed.set_field_at(status_index, name="Application Status", value=f"{result}\n{gear_status(reviewer)}\n▫️ Staff approval pending\n\u200b", inline=False)
                if next_index is not None:
                    embed.remove_field(next_index)
                set_gear_review(channel.id, message_id, reviewer)
                try:
                    await message.edit(content=form_content(channel.id, message.id), embed=embed, view=ApplicationView(self, rsn, verified=verified, submitted=bool(form_submission(channel.id, message.id)), separate_prompt=bool(get_form_prompt(channel.id)), trial=any(f.name == "Trial Member Graduation" for f in embed.fields)), allowed_mentions=discord.AllowedMentions.none())
                except Exception:
                    set_gear_review(channel.id, message_id, previous)
                    raise
                logger.info("Application gear review channel=%s staff=%s verified=%s", channel.id, interaction.user.id, verified)
                await notice(interaction, True, "Gear requirement marked as met." if verified else "Gear verification returned to pending.", ephemeral=True)
            except Exception:
                logger.exception("Application gear review failed for %s", channel.id)
                await notice(interaction, True, "Unable to update the gear review. Please try again.", ephemeral=True)

    async def refresh(self, interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != GUILD or channel.category_id != CATEGORY:
            await notice(interaction, False, "This report is only available inside an application ticket.", ephemeral=True)
            return
        row = get_report(channel.id)
        if not row or interaction.message.id != row[2]:
            await notice(interaction, False, "This report is no longer active.", ephemeral=True)
            return
        applicant_id, rsn, _ = row
        if interaction.user.id != applicant_id and not is_staff(interaction.user):
            await notice(interaction, False, "Only the applicant or staff may refresh this report.", ephemeral=True)
            return
        lock = self.locks.setdefault(channel.id, asyncio.Lock())
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with lock:
            if time.monotonic() - self.cooldowns.get(channel.id, -1000) < 60:
                await notice(interaction, True, "Please wait a minute between refreshes.", ephemeral=True)
                return
            if final_approval(channel.id, interaction.message.id):
                await notice(interaction, True, "This application has already been approved.", ephemeral=True)
                return
            self.cooldowns[channel.id] = time.monotonic()
            payload, note = await self.get_payload(rsn, applicant_id, update=True)
            if not payload:
                await notice(interaction, True, note or "WOM unavailable. Existing report retained.", ephemeral=True)
                return
            reviewer = gear_reviewer(channel.id, interaction.message.id)
            trial = await self.trial_context(channel, applicant_id)
            await interaction.message.edit(content=form_content(channel.id, interaction.message.id), embed=report_embed(rsn, payload, channel.id == TEST_CHANNEL, note, reviewer, trial=trial, logo_url=(channel.guild.icon or self.bot.user.display_avatar).url, applicant_id=applicant_id), view=ApplicationView(self, rsn, verified=bool(reviewer), submitted=bool(form_submission(channel.id, interaction.message.id)), separate_prompt=bool(get_form_prompt(channel.id)), trial=trial is not None), allowed_mentions=discord.AllowedMentions.none())
            await notice(interaction, True, "Eligibility report refreshed.", ephemeral=True)

async def setup(bot):
    initialize()
    cog = ApplicationCog(bot)
    await bot.add_cog(cog)
    bot.add_view(ApplicationPromptView(cog))
    bot.add_view(ApplicationView(cog))
    bot.add_view(ApplicationView(cog, verified=True))
    bot.add_view(ApplicationView(cog, trial=True))
    bot.add_view(ApplicationView(cog, verified=True, trial=True))
    bot.add_view(ApplicationView(cog, submitted=True))
    bot.add_view(ApplicationView(cog, submitted=True, verified=True))

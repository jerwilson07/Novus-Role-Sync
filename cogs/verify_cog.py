"""Replacement Novus Verify commands; no direct tenure or WOM-link writes."""
import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

from common import GUEST_ROLE_ID, CLAN_FRIEND_ROLE_ID
from cogs.tasks_cog import (
    NOVUS_GUILD_ID, NOVUS_COMMAND_ROLE_IDS, NOVUS_TENURE_ROLES,
    NOVUS_MOD_CHAT_CHANNEL_ID,
)

logger = logging.getLogger("WOMBot")


def unique_role(guild, name):
    matches = [r for r in guild.roles if r.name.casefold() == name.casefold()]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one Discord role named {name}.")
    return matches[0]


def parse_rsn(value):
    parts = [p.strip() for p in re.split(r"[|/]", value)]
    if any(not re.fullmatch(r"[A-Za-z0-9 _-]{1,12}", p) or not p.strip(" _-")
           for p in parts):
        raise ValueError("Each RSN must be 1–12 valid OSRS characters.")
    keys = [" ".join(p.replace("_", " ").replace("-", " ").split()).casefold()
            for p in parts]
    if len(set(keys)) != len(keys):
        raise ValueError("Please enter each account only once.")
    nickname = re.sub(r"\s*([|/])\s*", r" \1 ", value.strip())
    if len(nickname) > 32:
        raise ValueError("The combined nickname must be 32 characters or fewer.")
    return nickname


class VerifyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks = {}

    def lock(self, member):
        return self.locks.setdefault(member.id, asyncio.Lock())

    async def log_approval(self, guild, actor, member, already):
        channel = guild.get_channel(NOVUS_MOD_CHAT_CHANNEL_ID)
        if channel is None:
            logger.warning("Approval log channel unavailable")
            return False
        try:
            action = "Reviewed existing membership" if already else "Approved new member"
            await channel.send(
                f"**{action}:** {member.mention} ({discord.utils.escape_markdown(member.display_name)}) "
                f"by {actor.mention}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return True
        except discord.HTTPException:
            logger.exception("Could not log approval for %s", member.id)
            return False

    @app_commands.command(name="rsn", description="Set your server RSN and receive the Verified role.")
    @app_commands.describe(username="Your exact OSRS username; separate additional accounts with | or /")
    @app_commands.guild_only()
    async def rsn(self, interaction: discord.Interaction, username: str):
        guild = interaction.guild
        if guild is None or guild.id != NOVUS_GUILD_ID:
            await interaction.response.send_message("This command is only available in Novus.", ephemeral=True)
            return
        try:
            nickname = parse_rsn(username)
            verified = unique_role(guild, "Verified")
            guest = guild.get_role(GUEST_ROLE_ID)
            if guest is None:
                raise ValueError("Guest role is not configured.")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock(interaction.user):
                member = await guild.fetch_member(interaction.user.id)
                if member.nick != nickname:
                    await member.edit(nick=nickname, reason="Novus /rsn")
                if verified not in member.roles:
                    await member.add_roles(verified, reason="Novus /rsn")
                if guest in member.roles:
                    await member.remove_roles(guest, reason="Novus /rsn")
        except discord.HTTPException:
            logger.exception("RSN verification failed for %s", interaction.user.id)
            await interaction.followup.send("Could not complete verification. Please contact staff or retry; some changes may already have applied.", ephemeral=True)
            return
        await interaction.followup.send(f"Verified! Your server RSN is **{discord.utils.escape_markdown(nickname)}**.", ephemeral=True)

    @app_commands.command(name="approve", description="Approve a member and update their server roles.")
    @app_commands.describe(member="The applicant to approve")
    @app_commands.guild_only()
    async def approve(self, interaction: discord.Interaction, member: discord.Member):
        guild = interaction.guild
        if guild is None or guild.id != NOVUS_GUILD_ID:
            await interaction.response.send_message("This command is only available in Novus.", ephemeral=True)
            return
        try:
            founder = unique_role(guild, "Founder")
            staff_ids = set(NOVUS_COMMAND_ROLE_IDS) | {founder.id}
        except ValueError:
            staff_ids = set(NOVUS_COMMAND_ROLE_IDS)
            logger.warning("Founder role missing or ambiguous")
        if not any(r.id in staff_ids for r in interaction.user.roles):
            await interaction.response.send_message("Only Founder, Council, Moderators and Trial Moderators can approve members.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("You cannot approve a bot.", ephemeral=True)
            return
        try:
            full = unique_role(guild, "Member")
            squire = unique_role(guild, "Squire")
            trial = unique_role(guild, "Trial Member")
            verified = unique_role(guild, "Verified")
            friend = guild.get_role(CLAN_FRIEND_ROLE_ID)
            guest = guild.get_role(GUEST_ROLE_ID)
            if guest is None:
                raise ValueError("Guest role is not configured.")
            if friend is None:
                raise ValueError("Clan Friend role is not configured.")
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.lock(member):
                member = await guild.fetch_member(member.id)
                if verified not in member.roles:
                    await interaction.followup.send("This applicant must complete /rsn first.", ephemeral=True)
                    return
                already = full in member.roles
                add = []
                if not already:
                    add.append(full)
                if not any(r.name.strip().casefold() in NOVUS_TENURE_ROLES for r in member.roles):
                    add.append(squire)
                if add:
                    await member.add_roles(*add, reason=f"Novus /approve by {interaction.user.id}")
                remove = [r for r in (friend, trial, guest) if r in member.roles]
                if remove:
                    await member.remove_roles(*remove, reason=f"Novus /approve by {interaction.user.id}")
        except discord.HTTPException:
            logger.exception("Approval failed for %s", member.id)
            await interaction.followup.send("Could not complete approval. Please check roles and retry; some changes may already have applied.", ephemeral=True)
            return
        logged = await self.log_approval(guild, interaction.user, member, already)
        message = f"{member.mention} is approved. Existing ranks and Verified are preserved. WOM linking and tenure are handled by the existing sync."
        if not logged:
            message += " Staff log could not be sent."
        await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(VerifyCog(bot))

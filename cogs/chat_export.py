"""Owner-only, opt-in staff channel history export.

Discord integration permissions must explicitly allow the owner to see this
command. Default member permissions are disabled for everyone else.
"""
import io
import json
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

OWNER_ID = 534469721802670093
ALLOWED_CHANNEL_NAMES = {"council-chat", "mod-chat"}
MAX_MESSAGES = 1000
logger = logging.getLogger(__name__)


class ChatExport(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="exportchat", description="Private staff chat export")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=False)
    @app_commands.describe(
        channel="Staff channel to export",
        limit="Messages to export (1-1000); newest first",
        before="Optional message ID: export messages older than this ID",
    )
    async def exportchat(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        limit: app_commands.Range[int, 1, 1000] = 1000,
        before: str | None = None,
    ):
        if interaction.user.id != OWNER_ID:
            await interaction.response.send_message(
                "You are not authorized to use this command.", ephemeral=True
            )
            return

        if channel.guild.id != interaction.guild_id or channel.name not in ALLOWED_CHANNEL_NAMES:
            await interaction.response.send_message(
                "Only #council-chat and #mod-chat may be exported.", ephemeral=True
            )
            return

        if not self.bot.intents.message_content:
            await interaction.response.send_message(
                "Export is disabled until the Message Content intent is enabled "
                "in Discord's Developer Portal and EXPORT_MESSAGE_CONTENT_ENABLED=true "
                "is configured for this bot.",
                ephemeral=True,
            )
            return

        cursor = None
        if before:
            if not before.isdecimal():
                await interaction.response.send_message(
                    "The before field must be a Discord message ID.", ephemeral=True
                )
                return
            cursor = discord.Object(id=int(before))

        await interaction.response.defer(ephemeral=True, thinking=True)
        records = []
        try:
            async for message in channel.history(
                limit=limit, before=cursor, oldest_first=False
            ):
                records.append({
                    "id": str(message.id),
                    "author": message.author.name,
                    "author_id": str(message.author.id),
                    "created_at": message.created_at.isoformat(),
                    "content": message.content,
                    "reply_to": str(message.reference.message_id)
                    if message.reference and message.reference.message_id else None,
                    "attachments": [
                        {"filename": attachment.filename, "url": attachment.url}
                        for attachment in message.attachments
                    ],
                })
        except discord.Forbidden:
            await interaction.followup.send(
                "Bot lacks permission to view this channel or read its history.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logger.exception("Staff export Discord API error")
            await interaction.followup.send(
                "Discord could not complete this export.", ephemeral=True
            )
            return

        records.reverse()
        payload = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
        if len(payload) > 7_000_000:
            await interaction.followup.send(
                "Export exceeds the safe attachment size. Retry with a smaller limit.",
                ephemeral=True,
            )
            return

        filename = f"{channel.name}-{records[0]['id'] if records else 'empty'}.json"
        file = discord.File(io.BytesIO(payload), filename=filename)
        next_before = records[0]["id"] if records else None
        next_note = (
            f" To continue backward, run again with before:{next_before}."
            if next_before else ""
        )
        await interaction.followup.send(
            f"Exported {len(records)} messages from {channel.mention}."
            + next_note
            + " This is a private response; handle the file as confidential.",
            file=file,
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatExport(bot))

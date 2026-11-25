"""Structured, redacted runtime logs shared by the Novus Discord bots."""
import asyncio
import json
import logging
import os
import re
import sys

import discord
from discord import app_commands


_URL_QUERY = re.compile(r"""(https?://[^\s"'<>?]+)\?[^\s"'<>]*""")
_SECRET_NAMES = ("DISCORD_TOKEN", "DISCORD_BOT_TOKEN", "WOM_API_KEY", "DATABASE_URL")
_TASKS = set()


def _redact(value):
    value = _URL_QUERY.sub(r"\1?[redacted]", value)
    for name in _SECRET_NAMES:
        secret = os.getenv(name)
        if secret:
            value = value.replace(secret, "[redacted]")
    return value


class RailwayFormatter(logging.Formatter):
    def __init__(self, service):
        super().__init__()
        self.service = service

    def format(self, record):
        level = ("error" if record.levelno >= logging.ERROR else
                 "warn" if record.levelno >= logging.WARNING else
                 "info" if record.levelno >= logging.INFO else "debug")
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        return json.dumps({
            "level": level,
            "message": _redact(message),
            "service": self.service,
            "logger": record.name,
        }, ensure_ascii=False)


def configure_logging(service):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RailwayFormatter(service))
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO) if level in
        {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"} else logging.INFO,
        handlers=[handler],
    )


def report_interaction_error(interaction, error, action):
    logger = logging.getLogger("novus.interactions")
    args = (action, interaction.id, interaction.guild_id, interaction.channel_id)
    context = "%s failed | interaction_id=%s guild_id=%s channel_id=%s"
    if isinstance(error, (app_commands.CheckFailure, app_commands.TransformerError)):
        logger.debug(context + " | reason=%s", *args, type(error).__name__)
        return
    if isinstance(error, (app_commands.CommandNotFound, app_commands.CommandSignatureMismatch)):
        logger.warning(context + " | reason=%s", *args, type(error).__name__)
        return
    cause = getattr(error, "original", error)
    logger.error(context, *args, exc_info=(type(cause), cause, cause.__traceback__))


class LoggedCommandTree(app_commands.CommandTree):
    async def on_error(self, interaction, error):
        command = interaction.command
        report_interaction_error(interaction, error, getattr(command, "qualified_name", "slash command"))


class _ViewLogging:
    async def on_error(self, interaction, error, item):
        report_interaction_error(interaction, error, f"{type(self).__name__}.{type(item).__name__}")


class LoggedView(_ViewLogging, discord.ui.View):
    pass


class LoggedModal(discord.ui.Modal):
    async def on_error(self, interaction, error):
        report_interaction_error(interaction, error, type(self).__name__)


def start_logged_task(coro, *, name):
    task = asyncio.create_task(coro, name=name)
    _TASKS.add(task)

    def done(completed):
        _TASKS.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logging.getLogger("novus.tasks").error(
                "Background task failed | task=%s", completed.get_name(),
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(done)
    return task


# Shared Novus UI policy. Resolve the actual server emoji by name at runtime.
_UI_CLIENT = None
_NOVUS_GUILD = 1532817291048124466

def bind_novus_ui(client):
    global _UI_CLIENT
    _UI_CLIENT = client

NOVUS_CHECK = "<:Check:1154902522008445078>"

def check():
    return NOVUS_CHECK

async def _dismiss_private(message):
    await asyncio.sleep(10)
    try:
        if message.flags.ephemeral:
            await message.delete()
    except discord.HTTPException:
        pass

async def notice(interaction, followup, *args, **kwargs):
    """Auto-dismiss only short plain private messages, never views or evidence."""
    content = args[0] if args else kwargs.get("content")
    transient = (
        kwargs.get("ephemeral") is True
        and isinstance(content, str) and 0 < len(content) <= 300
        and not re.search(r"https?://|attachment://", content)
        and not any(kwargs.get(k) for k in ("embed", "embeds", "view", "file", "files", "attachments", "poll"))
        and not kwargs.get("wait") and "delete_after" not in kwargs
    )
    if followup:
        if transient:
            kwargs["wait"] = True
        message = await interaction.followup.send(*args, **kwargs)
        if transient and message is not None and getattr(getattr(message, "flags", None), "ephemeral", False):
            start_logged_task(_dismiss_private(message), name="novus-private-dismiss")
        return message
    if transient:
        kwargs["delete_after"] = 10
    return await interaction.response.send_message(*args, **kwargs)


async def migrate_existing_checks(client):
    """Update owned, visible, non-ephemeral message text/embeds, in place."""
    await asyncio.sleep(60)  # Let normal startup card refreshes finish first.
    guild = client.get_guild(_NOVUS_GUILD)
    if guild is None:
        return
    replacement = check()
    # Use the exact usable server emoji; never create a replacement emoji.
    import sqlite3
    state_path = os.path.join(os.getenv("NOVUS_DATA_DIR") or ("/data" if os.path.isdir("/data") and os.access("/data", os.W_OK) else "."), "novus-ui-migrations.sqlite3")
    key = f"status-history-v3:{client.user.id}:{guild.id}"
    with sqlite3.connect(state_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS migrations (key TEXT PRIMARY KEY)")
        if conn.execute("SELECT 1 FROM migrations WHERE key=?", (key,)).fetchone():
            return
    log = logging.getLogger("novus.ui")
    counts = {"channels":0,"messages":0,"edited":0,"unreadable":0,"errors":0,"component_only":0}
    def replace_fields(value):
        if isinstance(value, str):
            return value.replace("✅", replacement).replace("❌", "✖️")
        if isinstance(value, list):
            return [replace_fields(v) for v in value]
        if isinstance(value, dict):
            return {k:replace_fields(v) if k not in ("url","icon_url","proxy_url") else v for k,v in value.items()}
        return value
    def contains_component_check(message):
        return any(any(mark in json.dumps(c.to_dict(), ensure_ascii=False) for mark in ("✅", "❌")) for c in message.components)
    channels = {c.id:c for c in list(guild.text_channels) + list(guild.threads)}
    try:
        for thread in await guild.active_threads():
            channels[thread.id] = thread
    except discord.HTTPException:
        pass
    # Read only channels this bot can access. No cross-server access.
    for channel in channels.values():
        if guild.me is None:
            break
        permissions = channel.permissions_for(guild.me)
        if not permissions.view_channel or not permissions.read_message_history:
            counts["unreadable"] += 1
            continue
        counts["channels"] += 1
        try:
            async for message in channel.history(limit=None):
                if message.author.id != client.user.id or message.flags.ephemeral:
                    continue
                counts["messages"] += 1
                content = message.content
                edits = {}
                if "✅" in content or "❌" in content:
                    edits["content"] = content.replace("✅",replacement).replace("❌","✖️")
                old = [e.to_dict() for e in message.embeds]
                new = replace_fields(old)
                if new != old:
                    edits["embeds"] = [discord.Embed.from_dict(e) for e in new]
                if contains_component_check(message):
                    # A separate renderer owns the active view. Never replace
                    # callbacks by reconstructing a generic persistent view.
                    counts["component_only"] += 1
                if not edits:
                    continue
                if getattr(message.flags,"is_components_v2",False):
                    counts["component_only"] += 1
                    continue
                try:
                    # Omitting view and attachments retains all controls/files.
                    await message.edit(**edits,allowed_mentions=discord.AllowedMentions.none())
                    counts["edited"] += 1
                    await asyncio.sleep(0.3)
                except discord.NotFound:
                    continue
                except discord.HTTPException:
                    counts["errors"] += 1
        except discord.Forbidden:
            counts["unreadable"] += 1
        except Exception:
            counts["errors"] += 1
            log.exception("Check history scan failed for channel %s",channel.id)
        await asyncio.sleep(0.2)
    log.info("Novus status history migration complete %s",counts)
    if not counts["errors"]:
        with sqlite3.connect(state_path) as conn:
            conn.execute("INSERT OR IGNORE INTO migrations VALUES (?)",(key,))

def start_check_migration(client):
    if getattr(client,"_check_history_started",False):
        return
    client._check_history_started=True
    start_logged_task(migrate_existing_checks(client),name="novus-check-history")

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

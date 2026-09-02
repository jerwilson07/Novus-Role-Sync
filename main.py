import aiohttp
import discord
from discord.ext import commands
import sqlite3
import datetime
import logging
import os
import sys
import asyncio
from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from dotenv import load_dotenv
from common import WOM_API_KEY


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("WOMBot")


# ============================================================
# PROJECT / DATA PATHS
# ============================================================

# Keep code/config paths anchored to the project folder even if
# the process working directory changes.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
COGS_DIR = os.path.join(PROJECT_ROOT, "cogs")

# Load local environment variables when running on your PC.
# Railway environment variables work normally without config.env.
load_dotenv(
    dotenv_path=os.path.join(
        PROJECT_ROOT,
        "config.env"
    )
)


# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OWNER_ID = os.getenv("BOT_OWNER_ID")

# Railway:
#   Attach a persistent Volume and mount it at /data.
#   Set NOVUS_DATA_DIR=/data in Railway Variables.
#
# Local PC:
#   Leave NOVUS_DATA_DIR unset and the database/backups remain
#   inside the project folder.

configured_data_dir = os.getenv("NOVUS_DATA_DIR")

if configured_data_dir:
    DATA_DIR = os.path.abspath(
        configured_data_dir
    )
    STORAGE_MODE = "persistent"
else:
    DATA_DIR = PROJECT_ROOT
    STORAGE_MODE = "local"

try:
    os.makedirs(
        DATA_DIR,
        exist_ok=True
    )
except Exception as e:
    logger.critical(
        f"Could not create/access data directory "
        f"{DATA_DIR}: {e}"
    )
    sys.exit(1)

if not os.access(
    DATA_DIR,
    os.W_OK
):
    logger.critical(
        f"Data directory is not writable: "
        f"{DATA_DIR}"
    )
    sys.exit(1)

DB_PATH = os.path.join(
    DATA_DIR,
    "wom_multi.db"
)

BACKUP_DIR = os.path.join(
    DATA_DIR,
    "backups"
)

try:
    os.makedirs(
        BACKUP_DIR,
        exist_ok=True
    )
except Exception as e:
    logger.critical(
        f"Could not create/access backup directory "
        f"{BACKUP_DIR}: {e}"
    )
    sys.exit(1)

# Several existing cogs intentionally use relative paths such as
# "wom_multi.db" and "backups/".
#
# Running from DATA_DIR makes those existing relative paths use
# the persistent Railway Volume automatically without rewriting
# every cog.
os.chdir(
    DATA_DIR
)

logger.info(
    f"Storage mode: {STORAGE_MODE}"
)
logger.info(
    f"Data directory: {DATA_DIR}"
)
logger.info(
    f"SQLite database: {DB_PATH}"
)
logger.info(
    f"Backup directory: {BACKUP_DIR}"
)

if not TOKEN:
    logger.critical(
        "DISCORD_BOT_TOKEN environment variable "
        "not set. Exiting."
    )
    sys.exit(1)

if not WOM_API_KEY:
    logger.warning(
        "WOM_API_KEY environment variable not set. "
        "Running with Wise Old Man's "
        "unauthenticated rate limit."
    )

if not OWNER_ID:
    logger.warning(
        "BOT_OWNER_ID environment variable not set. "
        "Owner commands will not be available."
    )
    OWNER_ID = None
else:
    try:
        OWNER_ID = int(
            OWNER_ID
        )
    except ValueError:
        logger.critical(
            "BOT_OWNER_ID must be a valid integer. "
            "Exiting."
        )
        sys.exit(1)


# ============================================================
# DATABASE HELPERS
# ============================================================

def get_table_columns(
    cursor: sqlite3.Cursor,
    table_name: str
) -> set[str]:
    """
    Return the current column names for a SQLite table.
    """

    cursor.execute(
        f"PRAGMA table_info({table_name})"
    )

    return {
        column[1]
        for column in cursor.fetchall()
    }


# ============================================================
# DATABASE SETUP
# ============================================================

def init_db():
    conn = sqlite3.connect(
        DB_PATH
    )
    c = conn.cursor()

    # --------------------------------------------------------
    # BASE TABLES
    # --------------------------------------------------------

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS guild_configs (
            guild_id INTEGER PRIMARY KEY,
            group_id INTEGER,
            last_sync TEXT,
            inactive_since TEXT,
            log_channel_id INTEGER
        )
        """
    )

    # Primary RSN for each Discord member.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS links (
            guild_id INTEGER,
            discord_id INTEGER,
            rsn TEXT,
            wom_id INTEGER,
            PRIMARY KEY (guild_id, discord_id)
        )
        """
    )

    # Additional clan accounts belonging to the same Discord member.
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS alt_links (
            guild_id INTEGER,
            discord_id INTEGER,
            rsn TEXT,
            wom_id INTEGER,
            PRIMARY KEY (guild_id, discord_id, rsn)
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS role_mappings (
            guild_id INTEGER,
            wom_role TEXT,
            discord_role_id INTEGER,
            PRIMARY KEY (guild_id, wom_role)
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_stats (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

    # --------------------------------------------------------
    # DATABASE MIGRATIONS
    # --------------------------------------------------------

    guild_columns = get_table_columns(
        c,
        "guild_configs"
    )

    if "inactive_since" not in guild_columns:
        c.execute(
            "ALTER TABLE guild_configs "
            "ADD COLUMN inactive_since TEXT"
        )
        guild_columns.add(
            "inactive_since"
        )

    if "log_channel_id" not in guild_columns:
        c.execute(
            "ALTER TABLE guild_configs "
            "ADD COLUMN log_channel_id INTEGER"
        )
        guild_columns.add(
            "log_channel_id"
        )

    if "nickname_enforcement" not in guild_columns:
        c.execute(
            "ALTER TABLE guild_configs "
            "ADD COLUMN nickname_enforcement "
            "INTEGER DEFAULT 0"
        )
        guild_columns.add(
            "nickname_enforcement"
        )

    if "last_change_timestamp" not in guild_columns:
        c.execute(
            "ALTER TABLE guild_configs "
            "ADD COLUMN last_change_timestamp TEXT"
        )
        guild_columns.add(
            "last_change_timestamp"
        )

    if "reminder_interval_days" not in guild_columns:
        c.execute(
            "ALTER TABLE guild_configs "
            "ADD COLUMN reminder_interval_days "
            "INTEGER DEFAULT 7"
        )
        guild_columns.add(
            "reminder_interval_days"
        )

    if "dm_notifications_on" not in guild_columns:
        c.execute(
            "ALTER TABLE guild_configs "
            "ADD COLUMN dm_notifications_on "
            "INTEGER DEFAULT 0"
        )
        guild_columns.add(
            "dm_notifications_on"
        )

    link_columns = get_table_columns(
        c,
        "links"
    )

    if "wom_id" not in link_columns:
        c.execute(
            "ALTER TABLE links "
            "ADD COLUMN wom_id INTEGER"
        )
        link_columns.add(
            "wom_id"
        )

    if "dm_notifications_on" not in link_columns:
        c.execute(
            "ALTER TABLE links "
            "ADD COLUMN dm_notifications_on "
            "INTEGER DEFAULT 1"
        )
        link_columns.add(
            "dm_notifications_on"
        )

    conn.commit()
    conn.close()

    logger.info(
        "Database initialized successfully."
    )


# ============================================================
# BOT DEFINITION
# ============================================================

class WOMBot(
    commands.Bot
):
    def __init__(
        self
    ):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = False

        super().__init__(
            command_prefix="!",
            intents=intents,
            owner_id=OWNER_ID
        )

        self.http_session = None
        self.guild_command_cleanup_done = False

    async def on_ready(
        self
    ):
        logger.info(
            f"Logged in as "
            f"{self.user.name} "
            f"({self.user.id})"
        )

        logger.info(
            "Bot is online and ready!"
        )

        # One-time cleanup of old guild-specific slash commands.
        # The bot now uses global commands only.
        if not self.guild_command_cleanup_done:
            for guild in self.guilds:
                try:
                    self.tree.clear_commands(
                        guild=guild
                    )

                    await self.tree.sync(
                        guild=guild
                    )

                    logger.info(
                        f"Cleared old guild-specific "
                        f"commands from {guild.name}"
                    )

                except Exception as e:
                    logger.error(
                        f"Failed to clear guild-specific "
                        f"commands from {guild.name}: {e}"
                    )

            self.guild_command_cleanup_done = True

        # Only show CLI-related messages if running
        # in an interactive terminal.
        if sys.stdin.isatty():
            print(
                "------"
            )
            print(
                "Bot is running. Type commands "
                "below for maintenance."
            )
            print(
                "Available CLI commands: "
                "load, unload, reload, stop"
            )

    async def close(
        self
    ):
        if self.http_session:
            await self.http_session.close()

        await super().close()

    async def cli_loop(
        self
    ):
        """
        Handles command-line input for managing the bot.
        """

        await self.wait_until_ready()

        history = InMemoryHistory()
        session = PromptSession(
            history=history
        )

        while not self.is_closed():
            try:
                command = await session.prompt_async(
                    "> "
                )

                args = command.strip().split()

                if not args:
                    continue

                action = args[0].lower()

                if (
                    action
                    in [
                        "reload",
                        "load",
                        "unload"
                    ]
                    and len(args) > 1
                ):
                    cog_name = args[1]

                    try:
                        if action == "reload":
                            await self.reload_extension(
                                f"cogs.{cog_name}"
                            )

                        elif action == "load":
                            await self.load_extension(
                                f"cogs.{cog_name}"
                            )

                        elif action == "unload":
                            await self.unload_extension(
                                f"cogs.{cog_name}"
                            )

                        print(
                            f"✅ Successfully "
                            f"{action}ed cog: "
                            f"{cog_name}"
                        )

                    except Exception as e:
                        print(
                            f"❌ Error: {e}"
                        )

                elif action in [
                    "stop",
                    "shutdown",
                    "exit"
                ]:
                    print(
                        "Shutting down bot..."
                    )

                    await self.close()
                    break

                else:
                    print(
                        f"Unknown command: "
                        f"'{action}'. "
                        f"Available commands: "
                        f"load, unload, reload, stop"
                    )

            except (
                EOFError,
                KeyboardInterrupt
            ):
                logger.info(
                    "CLI loop interrupted. "
                    "Shutting down."
                )

                await self.close()
                break

    async def setup_hook(
        self
    ):
        init_db()

        self.http_session = (
            aiohttp.ClientSession()
        )

        # Load api_cog first. In Novus Sync this cog is a
        # lightweight placeholder because the original
        # Flask/Gunicorn dashboard is disabled.
        try:
            await self.load_extension(
                "cogs.api_cog"
            )

            logger.info(
                "Loaded cog: api_cog"
            )

        except Exception as e:
            logger.error(
                f"Failed to load cog "
                f"api_cog: {e}"
            )

        for filename in os.listdir(
            COGS_DIR
        ):
            if (
                filename.endswith(
                    ".py"
                )
                and filename != "__init__.py"
                and filename != "api_cog.py"
            ):
                try:
                    await self.load_extension(
                        f"cogs.{filename[:-3]}"
                    )

                    logger.info(
                        f"Loaded cog: "
                        f"{filename}"
                    )

                except Exception as e:
                    logger.error(
                        f"Failed to load cog "
                        f"{filename}: {e}"
                    )

        # Register commands globally.
        await self.tree.sync()

        logger.info(
            "Commands synced globally."
        )

        # Start CLI loop only in an interactive terminal.
        if sys.stdin.isatty():
            self.loop.create_task(
                self.cli_loop()
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    bot = WOMBot()

    bot.run(
        TOKEN,
        log_handler=None
    )

    logger.info(
        "Bot shut down gracefully."
    )

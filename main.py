import aiohttp
import discord
from novus_logging import check, notice, bind_novus_ui
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
from tenure_history import initialize_tenure_tables
from novus_logging import LoggedCommandTree, configure_logging, start_logged_task


# ============================================================
# LOGGING
# ============================================================

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

if __name__ == "__main__":
    configure_logging("Novus Role Sync")

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

    # Preserve cumulative clan tenure independently of Discord links.
    initialize_tenure_tables(c)

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
        intents.message_content = os.getenv("EXPORT_MESSAGE_CONTENT_ENABLED", "").lower() == "true"

        super().__init__(
            command_prefix="!",
            intents=intents,
            owner_id=OWNER_ID,
            tree_cls=LoggedCommandTree,
        )

        self.http_session = None
        self.guild_command_cleanup_done = False

    async def on_command_error(self, ctx, error):
        # Other bots (notably MEE6) own some ! commands in this server.
        # Do not log those messages as failures of Novus Role Sync.
        if isinstance(error, commands.CommandNotFound):
            return
        logger.error(
            "Unhandled prefix command error: %s", error,
            exc_info=(type(error), error, error.__traceback__),
        )

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

        # One-time Novus announcements requested on 2026-11-08.
        # Persistent bot_stats flags prevent reposting on later restarts.
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            announcements = [
                (
                    "manual_general_mod_recruiting_20261108_1",
                    1532817292205883514,
                    None,
                    (
                        "## ✦ Novus Moderator Applications\n"
                        "We're looking for members who want to help **foster engagement, "
                        "welcome and recruit new members, and keep Novus active and involved**.\n\n"
                        "If you'd like to take a more active role in the community, apply for "
                        "**Moderator** by opening a ticket in <#1533481323883401216>.\n\n"
                        "We're looking for people who are active, level-headed, approachable, "
                        "and willing to consistently contribute to the clan."
                    ),
                ),
                (
                    "manual_general_pb_submissions_20261108_1",
                    1532817292205883514,
                    None,
                    (
                        "## ✦ Novus PB Submissions\n"
                        "The Novus PB leaderboard is open for submissions in <#1568280192936443934>.\n\n"
                        "**PB rules:**\n"
                        "• Only PBs achieved **November 7, 2026 or later** are eligible.\n"
                        "• The PB must have been achieved while you were a **Novus member**.\n"
                        "• For group PBs, **every participant** must have been a Novus member "
                        "on the date of the run.\n"
                        "• Include the **PB date, completion time, activity, mode/team size, "
                        "and all participants**.\n"
                        "• Provide **dated proof** such as a screenshot, clip, or evidence URL. "
                        "Staff will verify eligibility and proof before approval.\n\n"
                        "Head to <#1568280192936443934> and use **Submit PB** to send your record for review."
                    ),
                ),
                (
                    "novus_trial_vouch_staff_20261123_v1",
                    1532835276567613531,
                    None,
                    (
                        "<@&1532830356875513906> <@&1532830960062693396> <@&1533587986204069908>\n"
                        "## ✦ New Trial Member Vouch System\n"
                        "Novus Sync now supports **/vouch** for prospective trial members who need "
                        "references before their membership review.\n\n"
                        "**How it works**\n"
                        "• **Member+** can submit `/vouch`, selecting the **Trial Member** "
                        "and adding optional comments about their PvM ability, teamwork, or mechanics.\n"
                        "• The bot **automatically records the Discord member who submits the command**. "
                        "Nobody selects a separate voucher.\n"
                        "• Each member may vouch **once per trial member**; duplicate vouches are rejected. "
                        "Trial members and non-clan members cannot submit vouches.\n"
                        "• When a trial member earns **3 unique vouches**, Novus Sync automatically "
                        "posts a staff alert in this channel showing all three references and comments.\n"
                        "• Staff can review saved feedback anytime with "
                        "**/vouches trial_member:@TrialMember**.\n\n"
                        "**Three vouches trigger a staff review, not automatic approval.** "
                        "Please verify the references and remaining membership requirements "
                        "before making a decision."
                    ),
                ),
                (
                    "novus_trial_member_hub_vouch_20261123_v1",
                    1544747251983843349,
                    None,
                    (
                        "## ✦ Trial Member Vouches\n"
                        "Trial members need **3 vouches from different Novus members** "
                        "who have raided with them before staff can review them for full membership.\n\n"
                        "**How to get vouched:** Raid with clan members and ask anyone "
                        "who can genuinely speak to your PvM ability, mechanics, and teamwork "
                        "to use `/vouch` and select your Discord account. "
                        "They can include optional feedback. Each member can vouch once.\n\n"
                        "Once you receive **3 unique vouches**, staff is automatically notified "
                        "to review your trial and remaining requirements. "
                        "**Three vouches do not guarantee approval.**\n\n"
                        "Looking for a team? Head to <#1532828125048733777>!"
                    ),
                ),
            ]

            for key, channel_id, channel_name, message in announcements:
                cursor.execute(
                    "SELECT value FROM bot_stats WHERE key = ?",
                    (key,)
                )
                if cursor.fetchone() is not None:
                    continue

                target_channel = None
                if channel_id is not None:
                    target_channel = self.get_channel(channel_id)
                    if target_channel is None:
                        try:
                            target_channel = await self.fetch_channel(channel_id)
                        except discord.HTTPException:
                            target_channel = None
                else:
                    for guild in self.guilds:
                        target_channel = discord.utils.get(
                            guild.text_channels,
                            name=channel_name
                        )
                        if target_channel:
                            break

                if target_channel is None:
                    logger.warning(
                        "One-time announcement %s was not sent: target channel not found.",
                        key
                    )
                    continue

                await target_channel.send(message)
                cursor.execute(
                    "INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?, ?)",
                    (
                        key,
                        datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat()
                    )
                )
                conn.commit()
                logger.info(
                    "Sent one-time announcement %s to #%s (%s).",
                    key,
                    target_channel.name,
                    target_channel.id
                )

            conn.close()
        except Exception:
            logger.exception(
                "Failed one-time Novus announcement post."
            )


        # One-time concise formatting update to the existing pinned raid-trial guide.
        # Only edit the known bot-owned message; never post a duplicate.
        try:
            update_key = "novus_raidtrial_hub_format_20261123_v5"
            conn = sqlite3.connect(DB_PATH)
            try:
                already_updated = conn.execute(
                    "SELECT 1 FROM bot_stats WHERE key = ?", (update_key,)
                ).fetchone() is not None
            finally:
                conn.close()

            if not already_updated:
                trial_hub_channel_id = 1544747251983843349
                original_message_id = 1574465292791320678
                trial_hub = self.get_channel(trial_hub_channel_id)
                if trial_hub is None:
                    trial_hub = await self.fetch_channel(trial_hub_channel_id)
                original_post = await trial_hub.fetch_message(original_message_id)
                if (
                    original_post.author.id != self.user.id
                    or not original_post.content.startswith("## ✦ Raid Trial Vouches")
                ):
                    logger.warning(
                        "Expected Raid Trial Vouches post not found; "
                        "no new message was sent."
                    )
                else:
                    await original_post.edit(content="## ✦ Raid Trial Vouches\n\nFor anyone completing **`!raidtrial`**:\n\n• Get **3 vouches** from different Novus members you've raided with.\n• Have each member use **`/vouch`** and select your Discord name.\n• At **3/3**, staff is notified to review your trial.\n\n-# Vouches do not guarantee membership approval.")
                    conn = sqlite3.connect(DB_PATH)
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?, ?)",
                            (
                                update_key,
                                datetime.datetime.now(
                                    datetime.timezone.utc
                                ).isoformat()
                            )
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    logger.info(
                        "Simplified original Raid Trial Vouches post in "
                        "#trial-member-hub (%s); message %s.",
                        trial_hub_channel_id, original_message_id
                    )
        except Exception:
            logger.exception(
                "Could not simplify original Raid Trial Vouches post; "
                "will retry on next startup."
            )

        # Update the existing staff-side vouch announcement in place.
        # Never repost it or repeat role pings when changing its wording.
        try:
            update_key = "novus_raidtrial_mod_post_edit_20261123_v2"
            conn = sqlite3.connect(DB_PATH)
            try:
                done = conn.execute(
                    "SELECT 1 FROM bot_stats WHERE key = ?", (update_key,)
                ).fetchone() is not None
            finally:
                conn.close()
            if not done:
                mod_channel_id = 1532835276567613531
                mod_channel = self.get_channel(mod_channel_id)
                if mod_channel is None:
                    mod_channel = await self.fetch_channel(mod_channel_id)

                original_post = None
                async for msg in mod_channel.history(limit=1000):
                    if (
                        msg.author.id == self.user.id
                        and "## ✦ New Trial Member Vouch System" in msg.content
                        and "Three vouches trigger a staff review" in msg.content
                    ):
                        original_post = msg
                        break

                if original_post is None:
                    logger.warning(
                        "Original staff vouch announcement not found; "
                        "skipping edit rather than posting a duplicate."
                    )
                else:
                    await original_post.edit(
                        content="## ✦ Raid Trial Vouch System\n**For applicants undergoing `!raidtrial`:**\n\n• **`/vouch`** — Any full Novus Member+ can vouch for a Trial Member they've raided with. Comments are optional; the bot records who submitted it.\n• **One vouch per member** — Duplicate vouches won't count.\n• **Each vouch** — A brief progress update (1/3, 2/3, etc.) appears here without pinging staff.\n• **3 unique vouches** — Staff are tagged with the references and comments for review.\n• **`/vouches`** — Staff can select a current Trial Member to review saved feedback.\n\n-# Vouches trigger staff review, not automatic approval. The bot checks the Trial Member role; staff should confirm `!raidtrial` eligibility.",
                        allowed_mentions=discord.AllowedMentions.none()
                    )
                    conn = sqlite3.connect(DB_PATH)
                    try:
                        conn.execute(
                            "INSERT OR REPLACE INTO bot_stats (key, value) VALUES (?, ?)",
                            (
                                update_key,
                                datetime.datetime.now(
                                    datetime.timezone.utc
                                ).isoformat()
                            )
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    logger.info(
                        "Updated original raid-trial vouch staff post in "
                        "#mod-chat (%s); message %s.",
                        mod_channel_id, original_post.id
                    )
        except Exception:
            logger.exception(
                "Could not edit original #mod-chat vouch announcement; "
                "will retry on next startup."
            )

        # Keep slash commands global only. Guild-specific copies would appear
        # alongside the same global commands and create duplicate entries in
        # Discord's command picker.
        if not self.guild_command_cleanup_done:
            for guild in self.guilds:
                try:
                    self.tree.clear_commands(
                        guild=guild
                    )

                    synced = await self.tree.sync(
                        guild=guild
                    )

                    logger.info(
                        "Cleared guild-specific slash commands from %s "
                        "(%s remain guild-scoped).",
                        guild.name,
                        len(synced)
                    )

                except Exception as e:
                    logger.error(
                        f"Failed to clear guild-specific "
                        f"commands for {guild.name}: {e}"
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
                            f"{check()} Successfully "
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

        except Exception:
            logger.exception("Failed to load cog api_cog")

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

                except Exception:
                    logger.exception("Failed to load cog %s", filename)

        # Keep the operational Novus staff commands visible while leaving
        # configuration/maintenance commands implemented but hidden.
        # /linkuser is a staff-only manual account-linking command.
        allowed_commands = {
            "audit",
            "syncall",
            "autolink",
            "linkuser",
            "trials",
            "tenure",
            "promotions",
            "vouch",
            "vouches",
            "exportchat",
            "rsn",
            "approve",
        }
        registered_commands = self.tree.get_commands()
        registered_names = {command.name for command in registered_commands}
        missing_commands = allowed_commands - registered_names

        if missing_commands:
            # Do not accidentally erase working Discord slash commands if
            # a required cog failed to load at startup.
            logger.error(
                "Refusing to replace Discord commands: required commands "
                "were not loaded: %s", sorted(missing_commands)
            )
        else:
            for command in registered_commands:
                if command.name not in allowed_commands:
                    # Local discord.py app_commands.Command instances
                    # do not have .type (unlike fetched AppCommand models).
                    # Every Novus command is a standard slash command.
                    self.tree.remove_command(command.name)

            synced_commands = await self.tree.sync()
            for command in synced_commands:
                if command.name in {"rsn", "approve"}:
                    logger.info("Novus command mention: </%s:%s>", command.name, command.id)

            logger.info(
                "Registered Novus operational commands: %s",
                ", ".join(sorted(allowed_commands))
            )

        # Start CLI loop only in an interactive terminal.
        if sys.stdin.isatty():
            start_logged_task(self.cli_loop(), name="sync-local-cli")


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    bot = WOMBot()
    bind_novus_ui(bot)

    bot.run(
        TOKEN,
        log_handler=None
    )

    logger.info(
        "Bot shut down gracefully."
    )

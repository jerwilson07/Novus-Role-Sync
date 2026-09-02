import logging

from discord.ext import commands


logger = logging.getLogger("WOMBot")


class ApiCog(commands.Cog):
    """
    Placeholder cog for the original project's web API/dashboard.

    Novus Sync does not currently use the Flask/Gunicorn web dashboard,
    so the web server has been disabled.

    Keeping this cog allows the existing cog loader to continue loading
    api_cog.py normally without starting any additional processes.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        logger.info(
            "Web dashboard disabled. "
            "Novus Sync is running in Discord-only mode."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(
        ApiCog(bot)
    )
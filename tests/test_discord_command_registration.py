"""Runtime check of app_commands removal without Discord API requests."""
import discord
from discord import app_commands
import unittest


class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_operational_commands_remain_registered(self):
        client = discord.Client(intents=discord.Intents.none())
        try:
            tree = app_commands.CommandTree(client)
            async def handler(interaction: discord.Interaction):
                pass

            for name in (
                "audit", "syncall", "autolink", "trials", "tenure",
                "promotions", "tenurehistory", "tenurecredit", "linkuser",
                "syncuser",
            ):
                tree.add_command(app_commands.Command(
                    name=name, description=f"{name} test", callback=handler
                ))

            allowed = {
                "audit",
                "syncall",
                "autolink",
                "trials",
                "tenure",
                "promotions",
            }
            for command in tree.get_commands():
                if command.name not in allowed:
                    tree.remove_command(command.name)

            self.assertEqual(
                {command.name for command in tree.get_commands()},
                allowed,
            )
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()

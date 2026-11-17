import asyncio
import json
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from discord import app_commands
from novus_logging import RailwayFormatter, report_interaction_error, start_logged_task


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    def test_structured_log_preserves_traceback_and_redacts(self):
        try:
            raise RuntimeError("secret-example https://example.test/proof?signature=hidden")
        except RuntimeError as error:
            row = logging.LogRecord(
                "novus.test", logging.ERROR, __file__, 1, "request failed", (),
                (type(error), error, error.__traceback__),
            )
        with patch.dict(os.environ, {"DISCORD_TOKEN": "secret-example"}):
            line = RailwayFormatter("test").format(row)
        self.assertEqual(len(line.splitlines()), 1)
        data = json.loads(line)
        self.assertEqual(data["level"], "error")
        self.assertEqual(data["service"], "test")
        self.assertIn("Traceback", data["message"])
        self.assertIn("https://example.test/proof?[redacted]", data["message"])
        self.assertNotIn("secret-example", line)
        self.assertNotIn("signature=hidden", line)

    def test_interaction_context_and_expected_denial(self):
        i = SimpleNamespace(id=1, guild_id=2, channel_id=3)
        with self.assertLogs("novus.interactions", level="DEBUG") as captured:
            report_interaction_error(i, app_commands.CheckFailure("denied"), "audit")
            report_interaction_error(i, RuntimeError("unexpected"), "audit")
        self.assertEqual([r.levelname for r in captured.records], ["DEBUG", "ERROR"])
        self.assertIn("interaction_id=1 guild_id=2 channel_id=3", captured.output[1])
        self.assertIn("RuntimeError: unexpected", captured.output[1])

    async def test_background_failure_is_named_and_cancel_is_quiet(self):
        async def fail():
            raise RuntimeError("task stopped")

        with self.assertLogs("novus.tasks", level="ERROR") as captured:
            failure = start_logged_task(fail(), name="example-task")
            cancelled = start_logged_task(asyncio.sleep(60), name="cancelled-task")
            cancelled.cancel()
            await asyncio.gather(failure, cancelled, return_exceptions=True)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("example-task", captured.output[0])
        self.assertIn("RuntimeError: task stopped", captured.output[0])


if __name__ == "__main__":
    unittest.main()

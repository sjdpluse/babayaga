from contextlib import redirect_stdout
from io import StringIO
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from truetrade.config import Settings
from truetrade.main import Worker
from truetrade.diagnostics import emit


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_credentials_are_visible_in_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker=Worker(Settings(state_dir=tmp))
            output=StringIO()
            try:
                with redirect_stdout(output): await worker.run(once=True)
                record=json.loads(output.getvalue())
                self.assertIn("credentials_not_configured",record["message"])
                self.assertEqual(record["event"],"connection_preflight")
            finally: worker.journal.close()

    async def test_auth_failure_visible_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            worker=Worker(Settings(state_dir=tmp,api_key="DO_NOT_LOG_KEY",api_secret="DO_NOT_LOG_SECRET"))
            output=StringIO()
            try:
                with patch("scripts.check_connection.check",new_callable=AsyncMock) as check, redirect_stdout(output):
                    check.return_value={"profile":{"status":401,"codes":["E_SECURITY_UNAUTHENTICATED"],"action":"check_allowlist_key_clock_then_signature"}}
                    await worker.run(once=True)
                raw=output.getvalue(); record=json.loads(raw)
                self.assertIn("401",record["message"])
                self.assertNotIn("DO_NOT_LOG_KEY",raw)
                self.assertNotIn("DO_NOT_LOG_SECRET",raw)
            finally: worker.journal.close()

    def test_success_and_action_survive_message_only_export(self):
        output=StringIO()
        with redirect_stdout(output):
            emit("connection_preflight",{"profile":"ok","futures_markets":"ok","exchange_writes_enabled":False})
        message=json.loads(output.getvalue())["message"]
        self.assertIn('"profile": "ok"',message)
        self.assertIn('"exchange_writes_enabled": false',message)

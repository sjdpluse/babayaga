import unittest
from unittest.mock import patch

from scripts.check_connection import check
from tests.test_exchange import FakeTransport, no_sleep
from truetrade.config import Settings
from truetrade.exchange.client import ExchangeClient, Response


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, responses):
        settings = Settings(api_key="TEST_KEY", api_secret="TEST_SECRET")
        transport = FakeTransport(responses)
        client = ExchangeClient(settings, transport, no_sleep)
        with patch("scripts.check_connection.ExchangeClient", return_value=client):
            result = await check(settings)
        return result, transport.requests

    async def test_both_403_results_are_retained(self):
        result, requests = await self.run_check([
            Response(403, {}, b'<html>profile forbidden</html>'),
            Response(403, {}, b'<html>markets forbidden</html>'),
        ])
        self.assertEqual(result["profile"]["status"], 403)
        self.assertEqual(result["futures_markets"]["status"], 403)
        self.assertEqual(result["futures_markets"]["response"]["endpoint"], "/futures/markets")
        self.assertEqual(len(requests), 2)

    async def test_profile_scope_failure_can_still_check_markets(self):
        result, _ = await self.run_check([Response(403, {}, b'{}'), Response(200, {}, b'[]')])
        self.assertEqual(result["profile"]["status"], 403)
        self.assertEqual(result["futures_markets"], "ok")

    async def test_401_stops_further_authenticated_calls(self):
        result, requests = await self.run_check([Response(401, {}, b'{}')])
        self.assertEqual(result["futures_markets"], "skipped_after_profile_failure")
        self.assertEqual(len(requests), 1)

    async def test_success_preserved(self):
        result, _ = await self.run_check([Response(200, {}, b'{}'), Response(200, {}, b'[]')])
        self.assertEqual(result["profile"], "ok")
        self.assertEqual(result["futures_markets"], "ok")
        self.assertFalse(result["exchange_writes_enabled"])

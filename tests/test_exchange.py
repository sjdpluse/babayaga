import asyncio
import hashlib
import hmac
import json
import unittest
from urllib.parse import urlsplit
from truetrade.config import Settings
from truetrade.exchange.client import ExchangeClient, Response, signature, retry_delay, DemoContractUnverified, ExchangeError
from truetrade.exchange.data import parse_udf


class FakeTransport:
    def __init__(self, responses): self.responses, self.requests = list(responses), []
    async def send(self, *args):
        self.requests.append(args)
        result = self.responses.pop(0)
        if isinstance(result, Exception): raise result
        return result


async def no_sleep(_): pass


class ExchangeTests(unittest.IsolatedAsyncioTestCase):
    def client(self, responses):
        t = FakeTransport(responses)
        s = Settings(api_key="TEST_KEY", api_secret="TEST_SECRET")
        return ExchangeClient(s, t, no_sleep, lambda: 1700000000.123), t

    async def test_exact_transmitted_query_signed(self):
        client, t = self.client([Response(200, {}, b'{}')])
        await client.request("get", "/futures/udf/history", {"symbol": "A/B +", "from": 5})
        method, url, headers, body, _ = t.requests[0]
        split = urlsplit(url)
        uri = split.path + "?" + split.query
        expected = hmac.new(b"TEST_SECRET", ("1700000000123GET"+uri).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(headers["X-Signature"], expected)
        self.assertEqual(headers["X-Timestamp"], "1700000000123")
        self.assertIsNone(body)

    async def test_retry_429_then_success(self):
        client, t = self.client([Response(429, {"Retry-After": "1"}, b'{}'), Response(200, {}, b'{"ok":true}')])
        self.assertEqual(await client.profile(), {"ok": True})
        self.assertEqual(len(t.requests), 2)

    async def test_auth_never_retried_or_body_echoed(self):
        client, t = self.client([Response(401, {}, b'{"errors":[{"code":"E_SECURITY_UNAUTHENTICATED","message":"SECRET_VALUE"}]}')])
        with self.assertRaises(ExchangeError) as e: await client.profile()
        self.assertNotIn("SECRET_VALUE", str(e.exception))
        self.assertEqual(len(t.requests), 1)
        self.assertIn("allowlist", e.exception.action)

    async def test_writes_blocked_even_in_demo_mode(self):
        client, t = self.client([])
        for method in ("POST", "PATCH", "DELETE"):
            with self.assertRaises(DemoContractUnverified):
                await client.request(method, "/futures/positions", body={"walletType":"debit"})
        self.assertEqual(t.requests, [])

    async def test_html_forbidden_metadata_does_not_echo_secrets_or_retry(self):
        client, t = self.client([Response(403, {
            "Content-Type": "text/html; TEST_SECRET", "Server": "cloudflare",
            "CF-Ray": "TEST_SECRET", "CF-Mitigated": "challenge",
        }, b'<!DOCTYPE html><html>TEST_KEY TEST_SECRET</html>')])
        with self.assertRaises(ExchangeError) as caught:
            await client.profile()
        details = caught.exception.diagnostic()
        self.assertEqual(details["response"]["body_kind"], "html")
        self.assertEqual(details["response"]["content_type"], "text/html")
        self.assertTrue(details["response"]["challenge_header_present"])
        self.assertEqual(details["response"]["endpoint"], "/users/profile")
        self.assertNotIn("TEST_KEY", json.dumps(details))
        self.assertNotIn("TEST_SECRET", json.dumps(details))
        self.assertEqual(len(t.requests), 1)

    async def test_unknown_headers_are_not_logged(self):
        client, _ = self.client([Response(403, {
            "Content-Type": "TEST_SECRET", "Server": "TEST_KEY",
        }, b'')])
        with self.assertRaises(ExchangeError) as caught:
            await client.markets()
        raw = json.dumps(caught.exception.diagnostic())
        self.assertNotIn("TEST_SECRET", raw)
        self.assertNotIn("TEST_KEY", raw)
        self.assertEqual(caught.exception.metadata["body_kind"], "empty")

    async def test_transfer_read_disallowed(self):
        client, t = self.client([])
        with self.assertRaises(ValueError): await client.request("GET", "/accounting/transfer")
        self.assertEqual(t.requests, [])

    async def test_clock_skew_blocks(self):
        client, _ = self.client([Response(200, {"Date": "Tue, 14 Nov 2023 20:00:00 GMT"}, b'{}')])
        with self.assertRaises(ExchangeError) as e: await client.profile()
        self.assertEqual(e.exception.action, "synchronize_host_clock")

    async def test_missing_keys_fails_before_network(self):
        client = ExchangeClient(Settings(), FakeTransport([]))
        with self.assertRaises(ValueError): await client.profile()

    def test_signature_known_vector_body_not_an_input(self):
        expected = hmac.new(b"secret", b"1700000000000POST/futures/positions", hashlib.sha256).hexdigest()
        self.assertEqual(signature("secret", 1700000000000, "post", "/futures/positions"), expected)

    def test_long_retry_after_requires_cooldown(self):
        with self.assertRaises(ExchangeError): retry_delay({"retry-after":"600"}, 0)

    def test_live_config_disallowed(self):
        with self.assertRaises(ValueError): Settings(mode="live")

    def test_udf_drops_unclosed_candles(self):
        payload = {"s":"ok", "t":[0,60,120], "o":[10]*3, "h":[11]*3, "l":[9]*3, "c":[10]*3, "v":[1]*3}
        self.assertEqual(len(parse_udf(payload, 150, 60).close), 2)
        payload["t"] = [0,120,180]
        with self.assertRaises(ValueError): parse_udf(payload, 500, 60)

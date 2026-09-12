"""Signed HTTP client. Contract is supplied by the user, not independently certified.

Network I/O uses a small injectable stdlib transport in asyncio.to_thread.
The same fully encoded URI is both signed and transmitted. No redirects or writes.
"""
import asyncio
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import json
import random
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from truetrade.config import Settings

BASE_URL = "https://apiv2.thetruetrade.io"
READ_PATHS = frozenset({"/users/profile", "/futures/markets", "/futures/markets/stats",
    "/futures/markets/orderbook", "/futures/markets/trades", "/futures/markets/funding-history",
    "/futures/udf/history", "/futures/quote-rates", "/futures/assets", "/futures/assets/pnl",
    "/futures/positions", "/futures/orders", "/futures/trades"})


def signature(secret: str, timestamp_ms: int, method: str, uri: str) -> str:
    if not uri.startswith("/") or "#" in uri:
        raise ValueError("Signature requires an exact request path, optionally with query")
    return hmac.new(secret.encode(), f"{timestamp_ms}{method.upper()}{uri}".encode(), hashlib.sha256).hexdigest()


class ExchangeError(RuntimeError):
    def __init__(self, status: int, codes: tuple[str, ...], action: str, metadata=None):
        self.status, self.codes, self.action = status, codes, action
        self.metadata = metadata or {}
        # Never echo response body, request headers, raw URL or exception from a transport.
        super().__init__(f"Exchange HTTP {status}; codes={','.join(codes)}; action={action}")

    def diagnostic(self):
        return {"status": self.status, "codes": self.codes, "action": self.action,
                "response": self.metadata}


class DemoContractUnverified(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes


def response_metadata(response, path):
    """Only finite labels/counts: never export arbitrary headers or error pages."""
    headers = {k.lower(): v for k, v in response.headers.items()}
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    server = headers.get("server", "").strip().lower()
    prefix = response.body[:256].lstrip().lower()
    return {
        "endpoint": path if path in READ_PATHS else "other",
        "content_type": content_type if content_type in {
            "application/json", "text/html", "text/plain", "application/problem+json"
        } else "other_or_missing",
        "body_kind": "empty" if not response.body else (
            "html" if prefix.startswith((b"<!doctype html", b"<html")) else "other"),
        "body_bytes": len(response.body),
        "server": server if server in {"cloudflare", "nginx", "envoy", "awselb/2.0"}
                  else "other_or_missing",
        "cloudflare_header_present": "cf-ray" in headers,
        "challenge_header_present": headers.get("cf-mitigated", "").lower() == "challenge",
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    async def send(self, method, url, headers, body, timeout):
        def perform():
            request = Request(url, data=body, headers=headers, method=method)
            try:
                with build_opener(NoRedirect).open(request, timeout=timeout) as r:
                    return Response(r.status, dict(r.headers.items()), r.read(16_000_001))
            except HTTPError as e:
                return Response(e.code, dict(e.headers.items()), e.read(1_000_000))
        return await asyncio.to_thread(perform)


def retry_delay(headers, attempt, now=None):
    value = {k.lower(): v for k, v in headers.items()}.get("retry-after")
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
            except (ValueError, TypeError, OverflowError):
                delay = 0
        if delay > 60:
            raise ExchangeError(429, (), "cooldown_required")
        if delay > 0:
            return delay
    return min(30.0, 2 ** attempt + random.random())


class ExchangeClient:
    def __init__(self, settings: Settings, transport=None, sleep=asyncio.sleep, clock=time.time):
        self.settings = settings
        self.transport = transport or Transport()
        self.sleep, self.clock = sleep, clock
        self._lock = asyncio.Lock()
        self._last = -float("inf")

    async def request(self, method: str, path: str, params=None, body=None):
        method = method.upper()
        # Deliberately non-configurable until a reviewed exchange demo adapter exists.
        # A variable called DEMO=true or walletType=debit does not prove demo isolation.
        if method != "GET":
            raise DemoContractUnverified("Exchange writes blocked: official API demo routing and account proof are unavailable")
        if path not in READ_PATHS:
            raise ValueError("Endpoint is outside the read-only futures/profile allowlist")
        if body is not None:
            raise ValueError("GET body not permitted")
        if not self.settings.api_key or not self.settings.api_secret:
            raise ValueError("Set TRUETRADE_API_KEY and TRUETRADE_API_SECRET through environment variables")
        query = urlencode(params or {}, doseq=True)
        uri = path + ("?" + query if query else "")
        for attempt in range(self.settings.max_retries + 1):
            async with self._lock:
                delay = self.settings.request_interval - (time.monotonic() - self._last)
                if delay > 0:
                    await self.sleep(delay)
                timestamp = int(self.clock() * 1000)
                headers = {"X-API-Key": self.settings.api_key, "X-Timestamp": str(timestamp),
                           "X-Signature": signature(self.settings.api_secret, timestamp, method, uri)}
                self._last = time.monotonic()
                try:
                    response = await self.transport.send(method, BASE_URL + uri, headers, None, self.settings.timeout)
                except (OSError, TimeoutError):
                    if attempt == self.settings.max_retries:
                        raise ExchangeError(0, (), "network_unavailable") from None
                    response = None
            if response is None:
                await self.sleep(retry_delay({}, attempt))
                continue
            if response.status in {429, 500, 502, 503, 504} and attempt < self.settings.max_retries:
                await self.sleep(retry_delay(response.headers, attempt, self.clock()))
                continue
            if len(response.body) > 16_000_000:
                raise ExchangeError(response.status, (), "response_too_large")
            try:
                payload = json.loads(response.body)
            except (ValueError, UnicodeError):
                raise ExchangeError(response.status, (), "invalid_json",
                                    response_metadata(response, path)) from None
            if not 200 <= response.status < 300:
                errors = payload.get("errors", []) if isinstance(payload, dict) else []
                codes = tuple(e.get("code", "") for e in errors if isinstance(e, dict)
                              and re.fullmatch(r"E_[A-Z0-9_]{1,100}", str(e.get("code", ""))))
                action = {401: "check_allowlist_key_clock_then_signature", 403: "check_allowlist_scopes_and_key_active",
                          422: "reject_order_no_retry", 429: "cooldown_required"}.get(response.status, "halt_and_inspect_contract")
                metadata = response_metadata(response, path)
                metadata["body_kind"] = "json"
                raise ExchangeError(response.status, codes, action, metadata)
            date = {k.lower(): v for k, v in response.headers.items()}.get("date")
            if date:
                try:
                    skew = abs(parsedate_to_datetime(date).timestamp() - self.clock())
                except (ValueError, TypeError, OverflowError):
                    raise ExchangeError(response.status, (), "invalid_server_date") from None
                if skew > 25:
                    raise ExchangeError(response.status, (), "synchronize_host_clock")
            return payload
        raise ExchangeError(0, (), "retry_exhausted")

    async def profile(self): return await self.request("GET", "/users/profile")
    async def markets(self): return await self.request("GET", "/futures/markets")
    async def stats(self): return await self.request("GET", "/futures/markets/stats")
    async def orderbook(self, **params): return await self.request("GET", "/futures/markets/orderbook", params)
    async def market_trades(self, **params): return await self.request("GET", "/futures/markets/trades", params)
    async def funding(self, **params): return await self.request("GET", "/futures/markets/funding-history", params)
    async def history(self, **params): return await self.request("GET", "/futures/udf/history", params)
    async def quote_rates(self): return await self.request("GET", "/futures/quote-rates")
    async def assets(self, **params): return await self.request("GET", "/futures/assets", params)
    async def pnl(self, **params): return await self.request("GET", "/futures/assets/pnl", params)
    async def positions(self, **params): return await self.request("GET", "/futures/positions", params)
    async def orders(self, **params): return await self.request("GET", "/futures/orders", params)
    async def trades(self, **params): return await self.request("GET", "/futures/trades", params)

    async def open_position(self, body): return await self.request("POST", "/futures/positions", body=body)
    async def close_position(self, position_id, body=None): return await self.request("POST", f"/futures/positions/{position_id}/close", body=body)
    async def close_all_positions(self): return await self.request("POST", "/futures/positions/close-all")
    async def set_protection(self, position_id, body): return await self.request("PATCH", f"/futures/positions/{position_id}/tpsl", body=body)
    async def add_margin(self, position_id, body): return await self.request("PATCH", f"/futures/positions/{position_id}/add-margin", body=body)
    async def cancel_order(self, order_id): return await self.request("DELETE", f"/futures/orders/{order_id}")
    async def close_all_orders(self): return await self.request("POST", "/futures/orders/close-all")

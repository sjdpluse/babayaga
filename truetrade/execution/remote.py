"""Linux-safe signal client: send intent, never raw MT5 requests/client-chosen lots."""
import asyncio
import json
import os
from dataclasses import asdict
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError
from truetrade.brokers.base import OrderUncertain, OrderRejected, BrokerError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class SignalClient:
    def __init__(self, url, token):
        parsed = urlsplit(url)
        local = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname:
            raise ValueError("Agent requires HTTPS (HTTP only on loopback)")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("Agent URL must be an origin")
        if len(token) < 32:
            raise ValueError("Agent token too short")
        self.url, self.token = url.rstrip("/"), token
        self.opener = build_opener(NoRedirect())

    @classmethod
    def from_env(cls):
        return cls(os.environ["MT5_AGENT_URL"], os.environ["MT5_AGENT_TOKEN"])

    def _request(self, method, path, payload=None):
        body = None if payload is None else json.dumps(payload, default=str, allow_nan=False).encode()
        request = Request(self.url+path, data=body, method=method,
                          headers={"Authorization": "Bearer "+self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=25) as response:
                return json.loads(response.read(2000000))
        except HTTPError as error:
            if method == "POST" and path == "/signals":
                if error.code in {400, 401, 404, 422}:
                    raise OrderRejected("Agent rejected signal before execution") from None
                raise OrderUncertain("Agent execution unsettled; query original decision ID") from None
            raise BrokerError("Agent request rejected; inspect decision status") from None
        except Exception:
            if method == "POST" and path == "/signals":
                raise OrderUncertain("Response lost; query original decision ID, do not retry") from None
            raise BrokerError("Agent unavailable") from None

    async def submit(self, signal):
        signal.validate_time()
        return await asyncio.to_thread(self._request, "POST", "/signals", asdict(signal))

    async def execution_state(self):
        return await asyncio.to_thread(self._request, "GET", "/execution-state")

    async def decision(self, decision_id):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", decision_id):
            raise ValueError("Invalid decision ID")
        return await asyncio.to_thread(self._request, "GET", "/decisions/"+decision_id)

    async def market(self, symbol):
        return await asyncio.to_thread(self._request, "POST", "/market", {"symbol": symbol})

    async def candles(self, symbol, timeframe="M1", count=200):
        return await asyncio.to_thread(self._request, "POST", "/candles",
                                      {"symbol": symbol, "timeframe": timeframe, "count": count})

    async def research_contract(self, symbol):
        return await asyncio.to_thread(self._request, "POST", "/research-contract", {"symbol":symbol})

    async def history(self, symbol, timeframe, count, start):
        return await asyncio.to_thread(self._request, "POST", "/history",
                                      {"symbol":symbol,"timeframe":timeframe,"count":count,"start":start})

    async def outcome(self, decision_id):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}",decision_id):
            raise ValueError("Invalid decision ID")
        return await asyncio.to_thread(self._request,"GET","/outcome/"+decision_id)

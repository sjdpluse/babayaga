import asyncio
from truetrade.config import Settings
from truetrade.exchange.client import ExchangeClient, ExchangeError
from truetrade.diagnostics import emit


async def check(settings=None):
    client = ExchangeClient(settings or Settings.from_env())
    # First authenticated call always profile. 403 can indicate missing optional
    # readonly scope: that does not establish the futures scope is invalid.
    result = {"demo_routing_verified": False, "exchange_writes_enabled": False}
    try:
        await client.profile()
        result["profile"] = "ok"
    except ExchangeError as e:
        result["profile"] = e.diagnostic()
        if e.status != 403:
            result["futures_markets"] = "skipped_after_profile_failure"
            return result
    try:
        await client.markets()
        result["futures_markets"] = "ok"
    except ExchangeError as e:
        result["futures_markets"] = e.diagnostic()
    return result


def main():
    try:
        result = asyncio.run(check())
    except (ValueError, ExchangeError) as e:
        emit("connection_preflight", {"connection":"blocked", "reason":str(e)})
        raise SystemExit(2) from None
    emit("connection_preflight", result)
    if result.get("futures_markets") != "ok": raise SystemExit(2)


if __name__ == "__main__": main()

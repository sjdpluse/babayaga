import asyncio
import json
from truetrade.config import Settings
from truetrade.exchange.client import ExchangeClient, ExchangeError


async def check(settings=None):
    client = ExchangeClient(settings or Settings.from_env())
    # First authenticated call always profile. 403 can indicate missing optional
    # readonly scope: that does not establish the futures scope is invalid.
    result = {"demo_routing_verified": False, "exchange_writes_enabled": False}
    try:
        await client.profile()
        result["profile"] = "ok"
    except ExchangeError as e:
        result["profile"] = {"status":e.status,"codes":e.codes,"action":e.action}
        if e.status != 403: return result
    await client.markets()
    result["futures_markets"] = "ok"
    return result


def main():
    try:
        result = asyncio.run(check())
    except (ValueError, ExchangeError) as e:
        print(json.dumps({"connection":"blocked", "reason":str(e)}, ensure_ascii=False))
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("futures_markets") != "ok": raise SystemExit(2)


if __name__ == "__main__": main()

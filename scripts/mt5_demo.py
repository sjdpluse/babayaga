"""Inspect an agent or explicitly submit one signal to a VERIFIED demo agent.

Default is read-only. Prices are supplied by the operator, never invented here.
This utility cannot submit to an agent configured for live trading.
"""
import argparse
import asyncio
import json
import time
from truetrade.brokers.base import Signal, BrokerError
from truetrade.execution.remote import SignalClient


async def run(args):
    client = SignalClient.from_env()
    health = await asyncio.to_thread(client._request, "GET", "/health")
    if args.decision_status:
        return await client.decision(args.decision_status)
    market = await client.market(args.symbol)
    if not args.send:
        return {"health": health, "market": market, "orders_sent": 0}
    if health.get("mode") != "demo" or not health.get("execution_allowed"):
        raise BrokerError("Demo command requires an execution-enabled DEMO agent")
    if not all((args.side, args.stop, args.take_profit, args.decision_id)):
        raise ValueError("--send requires --side, --stop, --take-profit and a stable --decision-id")
    now = time.time()
    intent = Signal(args.decision_id, args.symbol, args.side, args.stop, args.take_profit,
                    args.risk, now, now+60)
    # Never retry. If interrupted, query --decision-status with this ID first.
    return await client.submit(intent)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--side", choices=["LONG", "SHORT"])
    parser.add_argument("--stop")
    parser.add_argument("--take-profit")
    parser.add_argument("--risk", default="0.005")
    parser.add_argument("--decision-id")
    parser.add_argument("--decision-status")
    parser.add_argument("--send", action="store_true")
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args())), default=str, indent=2))
    except (BrokerError, ValueError, KeyError) as error:
        # Defined exceptions contain sanitized diagnostics, never raw credentials.
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()

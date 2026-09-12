import argparse
import asyncio
from pathlib import Path
from truetrade.config import Settings
from truetrade.exchange.client import ExchangeClient
from truetrade.exchange.collector import collect


def main():
    p = argparse.ArgumentParser(description="Capture TheTrueTrade data using reviewed API mappings")
    p.add_argument("mapping"); p.add_argument("--start",type=int,required=True)
    p.add_argument("--end",type=int,required=True); p.add_argument("--output",default="data/captures")
    a = p.parse_args()
    client = ExchangeClient(Settings.from_env())
    async def run():
        from scripts.check_connection import check
        result = await check()
        if result.get("futures_markets") != "ok": raise ValueError("Connection preflight failed")
        return await collect(client,a.mapping,a.output,a.start,a.end)
    for path in asyncio.run(run()): print(path)


if __name__ == "__main__": main()

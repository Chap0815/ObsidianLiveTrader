#!/usr/bin/env python3
"""Hyperliquid TESTNET smoke: meta, mid, candles, optional account.

  py -3 scripts/hl_spike.py
  py -3 scripts/hl_spike.py --with-account   # needs HL_PRIVATE_KEY in .env

Faucet / UI: https://app.hyperliquid-testnet.xyz
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.hyperliquid.client import HyperliquidClient  # noqa: E402
from app.hyperliquid.errors import HyperliquidError  # noqa: E402


async def main(with_account: bool) -> int:
    s = get_settings()
    client = HyperliquidClient(
        private_key=s.hl_private_key,
        account_address=s.hl_account_address,
        testnet=s.hl_testnet,
        base_url=s.hl_base_url or None,
    )
    try:
        print("base_url", client.base_url, "testnet", client.testnet)
        ts = await client.ping()
        print("ping_ok", ts)
        coin = s.default_symbol.split("_")[0]
        meta = await client.contract_meta(coin)
        print(
            "meta",
            meta.symbol,
            "szUnit",
            meta.vol_unit,
            "maxLev",
            meta.max_leverage,
        )
        t = await client.ticker(coin)
        print("mid", t.last_price, "funding", t.funding_rate)
        kl = await client.klines(coin, "15m", limit_hint=20)
        print("candles", len(kl), "last_close", kl[-1].close if kl else None)

        if with_account:
            if not s.hl_private_key:
                print("FAIL: HL_PRIVATE_KEY empty")
                return 2
            snap = await client.account_snapshot()
            print(
                "equity",
                snap.get("equity_usdt"),
                "available",
                snap.get("available_usdt"),
                "positions",
                len(snap.get("positions") or []),
            )
        else:
            print("skip account (pass --with-account)")
        print("OK — set TRADING_ENABLED only after manual micro-order on testnet UI/SDK")
        return 0
    except HyperliquidError as e:
        print("HL error:", e)
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--with-account", action="store_true")
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.with_account)))

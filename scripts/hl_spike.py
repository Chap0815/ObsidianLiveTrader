#!/usr/bin/env python3
"""Hyperliquid TESTNET smoke: meta, mid, candles, optional account.

  py -3 scripts/hl_spike.py
  py -3 scripts/hl_spike.py --with-account   # needs HL_PRIVATE_KEY in .env

Faucet / UI: https://app.hyperliquid-testnet.xyz
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.hyperliquid.client import HyperliquidClient, _required_hl_coin  # noqa: E402
from app.hyperliquid.errors import HyperliquidError  # noqa: E402

HL_TESTNET_HOST = "api.hyperliquid-testnet.xyz"


def _safe_exception_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}; provider details suppressed"


def _testnet_client_error(client) -> str | None:
    """Validate the constructed client before any public or private read."""
    if getattr(client, "testnet", None) is not True:
        return "constructed client is not marked as Testnet"
    raw_url = str(getattr(client, "base_url", "") or "")
    parsed = urlparse(raw_url)
    try:
        port = parsed.port
    except ValueError:
        return "constructed client has an invalid Testnet endpoint"
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != HL_TESTNET_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return "constructed client does not target the canonical Testnet endpoint"
    return None


def _testnet_configuration_error(settings) -> str | None:
    """Return a fail-closed reason before this Testnet-only tool creates a client."""
    if getattr(settings, "exchange", None) != "hyperliquid":
        return "EXCHANGE must be hyperliquid for the Hyperliquid Testnet smoke"
    if getattr(settings, "hl_testnet", None) is not True:
        return (
            "HL_TESTNET must be true; refusing to contact Hyperliquid Mainnet "
            "from the Testnet smoke"
        )
    return None


def _account_summary(snapshot) -> tuple[float, float, int]:
    """Return only strict numeric balances and a validated position count."""
    if not isinstance(snapshot, dict):
        raise HyperliquidError("account snapshot has an invalid shape")
    values: list[float] = []
    for field in ("equity_usdt", "available_usdt"):
        raw = snapshot.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise HyperliquidError("account snapshot has invalid balances")
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise HyperliquidError("account snapshot has invalid balances")
        values.append(value)
    positions = snapshot.get("positions")
    if not isinstance(positions, list) or not all(
        isinstance(position, dict) for position in positions
    ):
        raise HyperliquidError("account snapshot has invalid positions")
    return values[0], values[1], len(positions)


def _require_market_symbol(value, coin: str, *, source: str) -> None:
    observed = getattr(value, "symbol", None)
    if type(observed) is not str or observed.strip().upper() != coin:
        raise HyperliquidError(f"{source} symbol does not match the requested coin")


async def main(with_account: bool) -> int:
    s = get_settings()
    configuration_error = _testnet_configuration_error(s)
    if configuration_error:
        print(f"FAIL: {configuration_error}")
        return 2
    if with_account and not s.hl_ready:
        print(
            "FAIL: --with-account requires a valid HL_PRIVATE_KEY and, when set, "
            "a valid HL_ACCOUNT_ADDRESS"
        )
        return 2
    try:
        coin = _required_hl_coin(s.default_symbol, field="probe")
    except HyperliquidError:
        print("FAIL: DEFAULT_SYMBOL is invalid for Hyperliquid")
        return 2

    # Keep the default public probe credential-free. This limits the client to
    # public data even if a future refactor accidentally reuses its instance for
    # another read; account credentials enter the client only after the explicit
    # --with-account opt-in above.
    client = None
    exit_code = 0
    try:
        client = HyperliquidClient(
            private_key=s.hl_private_key if with_account else "",
            account_address=s.hl_account_address if with_account else "",
            testnet=s.hl_testnet,
            base_url=s.hl_base_url or None,
        )
        client_error = _testnet_client_error(client)
        if client_error:
            print(f"FAIL: {client_error}")
            return 2
        print("base_url", client.base_url, "testnet", client.testnet)
        ts = await client.ping()
        print("ping_ok", ts)
        meta = await client.contract_meta(coin)
        _require_market_symbol(meta, coin, source="contract metadata")
        print(
            "meta",
            meta.symbol,
            "szUnit",
            meta.vol_unit,
            "maxLev",
            meta.max_leverage,
        )
        t = await client.ticker(coin)
        _require_market_symbol(t, coin, source="ticker")
        print("mid", t.last_price, "funding", t.funding_rate)
        kl = await client.klines(coin, "15m", limit_hint=20)
        print("candles", len(kl), "last_close", kl[-1].close if kl else None)

        if with_account:
            snap = await client.account_snapshot()
            equity, available, position_count = _account_summary(snap)
            print(
                "equity",
                equity,
                "available",
                available,
                "positions",
                position_count,
            )
        else:
            print("skip account (pass --with-account)")
        print("OK — use the app Preview → Confirm flow for any Testnet order")
        exit_code = 0
    except HyperliquidError as e:
        print("HL error:", _safe_exception_detail(e))
        exit_code = 1
    except Exception as e:  # noqa: BLE001 - never print a diagnostic traceback
        print("HL probe error:", _safe_exception_detail(e))
        exit_code = 1
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as e:  # noqa: BLE001 - keep CLI output secret-free
                print("HL client close error:", _safe_exception_detail(e))
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    p = argparse.ArgumentParser(
        description="Hyperliquid Testnet read-only smoke (never Mainnet)"
    )
    p.add_argument(
        "--with-account",
        action="store_true",
        help="Also read the configured Testnet account; never places an order",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(main(args.with_account)))

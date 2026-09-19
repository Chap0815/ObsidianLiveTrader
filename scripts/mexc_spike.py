#!/usr/bin/env python3
"""Read-only MEXC connectivity and contract-metadata probe.

MEXC does not provide a futures Testnet. This tool therefore never sets
leverage, places an order or cancels an order. Public connectivity and contract
metadata are checked by default. Add --with-account only when authenticated
asset and position reads are explicitly intended.

Usage (from the project root):
  .\\.venv\\Scripts\\python.exe scripts\\mexc_spike.py
  .\\.venv\\Scripts\\python.exe scripts\\mexc_spike.py --with-account

Never commits secrets and never prints credentials, signatures or tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.mexc.client import (  # noqa: E402
    MexcClient,
    _required_contract_symbol,
    map_position,
    usdt_balances,
)
from app.mexc.errors import MexcError  # noqa: E402

MEXC_HOST = "api.mexc.com"


def _safe_exception_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}; provider details suppressed"


def _canonical_client_error(client) -> str | None:
    """Validate the constructed client before credentials can reach a request."""
    raw_url = str(getattr(client, "base_url", "") or "")
    parsed = urlparse(raw_url)
    try:
        port = parsed.port
    except ValueError:
        return "constructed client has an invalid MEXC endpoint"
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != MEXC_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        return "constructed client does not target the canonical MEXC endpoint"
    return None


def _redact(obj, *, secret_values: tuple[str, ...] = ()):
    """Remove credential-shaped keys and configured values from diagnostics."""
    # Replace longer values first so a key that is a prefix of its secret cannot
    # expose the secret's remaining suffix in a diagnostic string or field name.
    secrets = tuple(
        sorted({value for value in secret_values if value}, key=len, reverse=True)
    )
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            safe_key = str(key)
            for secret in secrets:
                safe_key = safe_key.replace(secret, "[redacted]")
            normalized = "".join(
                character for character in safe_key.lower() if character.isalnum()
            )
            if any(
                marker in normalized
                for marker in (
                    "secret",
                    "signature",
                    "apikey",
                    "accesskey",
                    "authorization",
                    "password",
                    "privatekey",
                    "passphrase",
                    "credential",
                    "cookie",
                    "token",
                )
            ):
                result[safe_key] = "***"
            else:
                result[safe_key] = _redact(value, secret_values=secrets)
        return result
    if isinstance(obj, list):
        return [_redact(value, secret_values=secrets) for value in obj]
    if isinstance(obj, str):
        for secret in secrets:
            obj = obj.replace(secret, "[redacted]")
    return obj


def _asset_summary(assets) -> dict[str, object]:
    """Return a fixed, validated account balance view without raw provider fields."""
    if not isinstance(assets, list) or not all(
        isinstance(asset, dict) for asset in assets
    ):
        raise MexcError("account assets have an invalid shape")
    present = any(
        str(asset.get("currency") or "").upper() == "USDT" for asset in assets
    )
    equity, available = usdt_balances(assets)
    return {
        "currency": "USDT",
        "present": present,
        "equity": equity,
        "availableBalance": available,
    }


def _position_summaries(positions) -> list[dict[str, object]]:
    """Normalize every private position before selecting terminal-safe fields."""
    if not isinstance(positions, list) or not all(
        isinstance(position, dict) for position in positions
    ):
        raise MexcError("open positions have an invalid shape")
    summaries: list[dict[str, object]] = []
    for position in positions:
        mapped = map_position(position)
        symbol = _required_contract_symbol(mapped.get("symbol"))
        side = mapped.get("side")
        hold_volume = mapped.get("hold_vol")
        entry_price = mapped.get("entry_price")
        open_type = mapped.get("open_type")
        if (
            side not in ("long", "short")
            or not isinstance(hold_volume, (int, float))
            or isinstance(hold_volume, bool)
            or hold_volume <= 0
            or not isinstance(entry_price, (int, float))
            or isinstance(entry_price, bool)
            or entry_price <= 0
            or open_type not in ("isolated", "cross")
        ):
            raise MexcError("open position contains invalid required values")
        summaries.append(
            {
                "symbol": symbol,
                "side": side,
                "holdVolume": hold_volume,
                "entryPrice": entry_price,
                "leverage": mapped.get("leverage"),
                "openType": open_type,
            }
        )
    return summaries


def _require_market_symbol(value, symbol: str, *, source: str) -> None:
    observed = getattr(value, "symbol", None)
    if type(observed) is not str or observed.strip().upper() != symbol:
        raise MexcError(f"{source} symbol does not match the requested contract")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only MEXC probe (MEXC has no futures Testnet)"
    )
    parser.add_argument(
        "--with-account",
        action="store_true",
        help="Also read authenticated assets and positions",
    )
    return parser


async def run(*, with_account: bool) -> int:
    settings = get_settings()
    if getattr(settings, "exchange", None) != "mexc":
        print("REFUSE: EXCHANGE must be mexc before the MEXC probe can run.")
        return 2
    if with_account and not settings.mexc_ready:
        print(
            "REFUSE: --with-account requires configured MEXC API credentials."
        )
        return 2
    try:
        symbol = _required_contract_symbol(
            settings.default_symbol,
            error_message="DEFAULT_SYMBOL is invalid for MEXC",
        )
    except MexcError:
        print("REFUSE: DEFAULT_SYMBOL is invalid for MEXC.")
        return 2

    api_key = settings.mexc_api_key if with_account else ""
    api_secret = settings.mexc_api_secret if with_account else ""
    client = None
    exit_code = 0

    try:
        client = MexcClient(settings.mexc_base_url, api_key, api_secret)
        client_error = _canonical_client_error(client)
        if client_error:
            print(f"REFUSE: {client_error}")
            return 2
        print("=== 1. public ping ===")
        server_time = await client.ping()
        print(f"server_time_ms={server_time}")

        print(f"=== 2. public contract detail: {symbol} ===")
        meta = await client.contract_meta(symbol)
        _require_market_symbol(meta, symbol, source="contract metadata")
        print(
            f"symbol={meta.symbol} apiAllowed={meta.api_allowed} "
            f"contractSize={meta.contract_size} priceUnit={meta.price_unit} "
            f"volUnit={meta.vol_unit} minVol={meta.min_vol} maxVol={meta.max_vol} "
            f"maxLeverage={meta.max_leverage}"
        )

        print(f"=== 3. public ticker: {symbol} ===")
        ticker = await client.ticker(symbol)
        _require_market_symbol(ticker, symbol, source="ticker")
        print(f"last_price={ticker.last_price}")

        if not with_account:
            print(
                "=== authenticated reads skipped; use --with-account only when "
                "account access is intended ==="
            )
        else:
            print("=== 4. authenticated assets ===")
            assets = await client.assets()
            print(
                _redact(
                    _asset_summary(assets),
                    secret_values=(api_key, api_secret),
                )
            )

            print("=== 5. authenticated open positions ===")
            positions = await client.positions()
            summaries = _position_summaries(positions)
            print(f"count={len(summaries)}")
            for summary in summaries[:5]:
                print(
                    _redact(
                        summary,
                        secret_values=(api_key, api_secret),
                    )
                )
    except MexcError as exc:
        print(f"FAIL: MEXC read-only probe failed: {_safe_exception_detail(exc)}")
        exit_code = 1
    except Exception as exc:  # noqa: BLE001 - never print a diagnostic traceback
        print(f"FAIL: MEXC read-only probe failed: {_safe_exception_detail(exc)}")
        exit_code = 1
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - keep CLI output secret-free
                print(f"FAIL: MEXC client close failed: {_safe_exception_detail(exc)}")
                exit_code = 1
    return exit_code


def main() -> None:
    arguments = _build_parser().parse_args()
    raise SystemExit(asyncio.run(run(with_account=arguments.with_account)))


if __name__ == "__main__":
    main()

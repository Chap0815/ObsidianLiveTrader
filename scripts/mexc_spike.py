#!/usr/bin/env python3
"""Task 0 — Manual MEXC vertical spike (LIVE; uses .env keys).

Run ONLY when you intend to hit the real exchange with a trade-only key
(no withdraw). Do NOT invoke this from automated pytest.

Steps (documented):
  1. ping / server time
  2. contract/detail for DEFAULT_SYMBOL → apiAllowed, contractSize, units
  3. private assets
  4. open positions
  5. set leverage (isolated long default) on an apiAllowed symbol
  6. place minimum-vol limit order FAR from market + unique externalOid
  7. cancel that order
  8. print redacted request/response shapes

Usage (from project root):
  .\\.venv\\Scripts\\python.exe scripts\\mexc_spike.py
  .\\.venv\\Scripts\\python.exe scripts\\mexc_spike.py --dry-read   # no place/cancel
  .\\.venv\\Scripts\\python.exe scripts\\mexc_spike.py --place      # place+cancel far limit

Never commits secrets. Never prints ApiKey / secret / Signature.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

# Project root on path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.mexc.client import MexcClient  # noqa: E402
from app.mexc.errors import MexcError  # noqa: E402


def _redact(obj):
    """Drop anything that might look like a secret key in nested dumps."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(s in kl for s in ("secret", "signature", "apikey", "api_key", "authorization")):
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


async def run(place: bool, dry_read: bool) -> int:
    s = get_settings()
    if not s.mexc_ready:
        print("FAIL: MEXC_API_KEY / MEXC_API_SECRET not set in .env")
        return 2

    symbol = s.default_symbol
    client = MexcClient(s.mexc_base_url, s.mexc_api_key, s.mexc_api_secret)
    try:
        print("=== 1. ping ===")
        ts = await client.ping()
        print(f"server_time_ms={ts}")

        print(f"=== 2. contract_detail {symbol} ===")
        meta = await client.contract_meta(symbol)
        print(
            f"symbol={meta.symbol} apiAllowed={meta.api_allowed} "
            f"contractSize={meta.contract_size} priceUnit={meta.price_unit} "
            f"volUnit={meta.vol_unit} minVol={meta.min_vol} maxVol={meta.max_vol} "
            f"maxLeverage={meta.max_leverage}"
        )
        if not meta.api_allowed:
            print("STOP: apiAllowed=false — do not place via API on this symbol")
            return 3

        print("=== 3. assets ===")
        assets = await client.assets()
        usdt = next(
            (a for a in assets if str(a.get("currency", "")).upper() == "USDT"),
            None,
        )
        print(_redact(usdt or {"note": "no USDT row"}))

        print("=== 4. open positions ===")
        positions = await client.positions()
        print(f"count={len(positions)}")
        for p in positions[:5]:
            print(_redact({k: p.get(k) for k in (
                "symbol", "positionType", "holdVol", "holdAvgPrice", "leverage", "openType"
            )}))

        if dry_read or not place:
            print(
                "=== skip place/cancel "
                f"(dry_read={dry_read}, place={place}) ==="
            )
            print(
                "Re-run with --place to set leverage + far limit min order + cancel.\n"
                "Requires TRADING_ENABLED awareness: this script does NOT check the flag;\n"
                "it is a manual operator tool. Prefer keeping TRADING_ENABLED=false in the app."
            )
            return 0

        print("=== 5. set_leverage (isolated long, lev=5) ===")
        try:
            lev_resp = await client.set_leverage(symbol, 5, open_type=1, position_type=1)
            print(_redact(lev_resp))
        except MexcError as e:
            print(f"set_leverage warning: {e}")

        ticker = await client.ticker(symbol)
        last = float(ticker.last_price or 0)
        # Far below market for a long limit — unlikely to fill
        far_price = round(last * 0.5 / meta.price_unit) * meta.price_unit if meta.price_unit else last * 0.5
        vol = meta.min_vol if meta.min_vol > 0 else meta.vol_unit
        external_oid = f"spike-{uuid.uuid4().hex[:16]}"

        body = {
            "symbol": symbol,
            "price": far_price,
            "vol": vol,
            "side": 1,  # open long
            "type": 1,  # limit
            "openType": 1,  # isolated
            "leverage": 5,
            "externalOid": external_oid,
            # SL far below far_price (long) — documents whether exchange accepts field
            "stopLossPrice": round(far_price * 0.9 / meta.price_unit) * meta.price_unit
            if meta.price_unit
            else far_price * 0.9,
        }
        print("=== 6. place far limit WITH stopLossPrice ===")
        print("request:", _redact(body))
        try:
            placed = await client.place_order(body)
            print("response:", _redact(placed))
        except MexcError as e:
            print(f"PLACE FAILED: {e}")
            print("raw:", _redact(getattr(e, "raw", None)))
            print(
                "Document this in docs/superpowers/plans/mexc-spike-results.md. "
                "Do not enable TRADING_ENABLED until place/cancel works."
            )
            return 4

        order_id = None
        if isinstance(placed, dict):
            order_id = placed.get("orderId") or placed.get("data")
            if isinstance(order_id, dict):
                order_id = order_id.get("orderId")
            # SL echo check
            for k in ("stopLossPrice", "stop_loss_price"):
                if k in placed:
                    print(f"SL field echoed in place response: {k}={placed.get(k)}")

        print("=== 6b. open_stop_orders / open_orders (SL verify probe) ===")
        try:
            stops = await client.open_stop_orders(symbol)
            print(f"stop_orders count={len(stops)}")
            for srow in stops[:5]:
                print(_redact(srow))
        except MexcError as e:
            print(f"open_stop_orders probe failed (path may differ): {e}")
        try:
            opens = await client.open_orders(symbol)
            print(f"open_orders count={len(opens)}")
        except MexcError as e:
            print(f"open_orders: {e}")

        print("=== 7. cancel ===")
        if order_id is None:
            print("No orderId in place response — cancel skipped; check open orders manually")
            return 5
        try:
            cancelled = await client.cancel_order([order_id])
            print("cancel response:", _redact(cancelled))
        except MexcError as e:
            print(f"CANCEL FAILED: {e}")
            return 6

        print("=== OK: place path /api/v1/private/order/create + cancel proven ===")
        print(
            "Review stop_orders output above. If SL never appears, keep "
            "AUTO_FLATTEN_IF_SL_UNVERIFIED=true and do not trust naked create SL."
        )
        print(
            "Write results (redacted) to docs/superpowers/plans/mexc-spike-results.md "
            "before TRADING_ENABLED=true."
        )
        return 0
    finally:
        await client.aclose()


def main() -> None:
    p = argparse.ArgumentParser(description="MEXC Task 0 vertical spike (manual)")
    p.add_argument(
        "--place",
        action="store_true",
        help="Place far min limit + cancel (LIVE)",
    )
    p.add_argument(
        "--dry-read",
        action="store_true",
        help="Only ping/detail/assets/positions (default if --place omitted)",
    )
    args = p.parse_args()
    code = asyncio.run(run(place=args.place, dry_read=args.dry_read or not args.place))
    raise SystemExit(code)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Live Hyperliquid TESTNET order-lifecycle smoke test.

Exercises the REAL app code path (OrderService → HyperliquidClient) against the
live testnet exchange -- the one thing the mocked unit tests can never prove:

  A) MARKET entry with a protective SL  → confirm the stop is ACTUALLY on the
     exchange (not just "request echoed"). This is the check that would have
     caught the SL-verify regression before it hit the user.
  B) Non-marketable RESTING LIMIT       → confirm it rests as
     `placed_unfilled_resting`, is NOT falsely flattened/cancelled, and warns
     "LIMIT RUHT" (the unfilled_resting fix, verified live).

Always cleans up afterwards: closes any position it opened, cancels any order
it placed, and verifies the account is flat again.

Usage (from repo root, venv python):
  .venv/Scripts/python.exe scripts/hl_smoke_trade.py            # DRY-RUN (preview only, places nothing)
  .venv/Scripts/python.exe scripts/hl_smoke_trade.py --confirm  # LIVE: really places/closes on TESTNET

Safety:
  * HARD-REFUSES to run unless exchange=hyperliquid AND HL_TESTNET=true.
  * Needs HL_PRIVATE_KEY + TRADING_ENABLED=true for --confirm.
  * Uses a tiny (~$20 notional) position at low leverage.
  Faucet / UI: https://app.hyperliquid-testnet.xyz
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.exchange_factory import create_exchange_client  # noqa: E402
from app.hyperliquid.errors import HyperliquidError  # noqa: E402
from app.models import OrderTicket  # noqa: E402
from app.orders.service import OrderError, OrderService  # noqa: E402
from app.orders.tokens import PreviewStore  # noqa: E402

TARGET_NOTIONAL_USDT = 20.0  # tiny test position
TEST_LEVERAGE = 3


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool | None, detail: str = "") -> None:
        tag = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.rows.append((tag, name, detail))
        print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""))

    @property
    def failed(self) -> bool:
        return any(t == "FAIL" for t, _, _ in self.rows)

    def summary(self) -> None:
        print("\n" + "=" * 64)
        for tag, name, _ in self.rows:
            print(f"  {tag:<4} {name}")
        n_fail = sum(1 for t, _, _ in self.rows if t == "FAIL")
        n_pass = sum(1 for t, _, _ in self.rows if t == "PASS")
        n_skip = sum(1 for t, _, _ in self.rows if t == "SKIP")
        print("=" * 64)
        print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped")


def _round_px(px: float, unit: float) -> float:
    return round(px / unit) * unit if unit and unit > 0 else px


def _round_up_vol(raw: float, unit: float, min_vol: float) -> float:
    v = math.ceil(raw / unit) * unit if unit and unit > 0 else raw
    return max(v, min_vol or 0.0)


async def _open_order_ids(client, coin: str) -> list:
    """All open (resting) order ids on the coin, triggers included."""
    ids: list = []
    try:
        for o in await client.open_stop_orders(coin):
            oid = o.get("orderId") or o.get("oid")
            if oid is not None:
                ids.append(oid)
    except Exception:  # noqa: BLE001
        pass
    try:
        ex = client._get_exchange()  # type: ignore[attr-defined]
        info = ex.info if hasattr(ex, "info") else None
        addr = getattr(client, "account_address", None) or getattr(client, "_address", None)
        if info is not None and addr:
            for o in info.frontend_open_orders(addr):
                if str(o.get("coin", "")).upper() == coin.upper():
                    oid = o.get("oid")
                    if oid is not None and oid not in ids:
                        ids.append(oid)
    except Exception:  # noqa: BLE001
        pass
    return ids


async def _cleanup(client, svc: OrderService, coin: str, side: str, rep: Report) -> None:
    print("\n-- Cleanup --")
    # 1) Close any position we may have opened.
    try:
        pos = await client.positions(coin)
        hold = 0.0
        for p in pos or []:
            hold = float(p.get("holdVol") or p.get("positionAmt") or 0.0)
        if hold and abs(hold) > 0:
            out = await svc.close_position(symbol=coin, side=side)
            rep.add("cleanup: close position", bool(out.get("ok")), f"status={out.get('status')}")
        else:
            rep.add("cleanup: no position to close", True)
    except Exception as e:  # noqa: BLE001
        rep.add("cleanup: close position", False, f"{type(e).__name__}: {e}")
    # 2) Cancel every remaining open order (resting limit + any stop).
    try:
        ids = await _open_order_ids(client, coin)
        cancelled = []
        for oid in ids:
            try:
                await client.cancel_order([{"orderId": oid, "symbol": coin}])
                cancelled.append(oid)
            except Exception:  # noqa: BLE001
                pass
        rep.add("cleanup: cancel open orders", True, f"cancelled={cancelled or 'none'}")
    except Exception as e:  # noqa: BLE001
        rep.add("cleanup: cancel open orders", False, f"{type(e).__name__}: {e}")
    # 3) Verify flat.
    try:
        pos = await client.positions(coin)
        leftover = sum(abs(float(p.get("holdVol") or 0.0)) for p in (pos or []))
        ids = await _open_order_ids(client, coin)
        flat = leftover <= 1e-9 and not ids
        rep.add("cleanup: account flat", flat, f"pos={leftover} open_orders={len(ids)}")
    except Exception as e:  # noqa: BLE001
        rep.add("cleanup: verify flat", False, f"{type(e).__name__}: {e}")


async def run(live: bool) -> int:
    s = get_settings()
    rep = Report()

    # ── Safety gates ───────────────────────────────────────────────────
    if (s.exchange or "").lower() not in ("hyperliquid", "hl"):
        print(f"REFUSE: exchange is '{s.exchange}', not hyperliquid.")
        return 2
    if not s.hl_testnet:
        print("REFUSE: HL_TESTNET is false -- this script NEVER runs on mainnet.")
        return 2
    if live and not s.hl_private_key:
        print("REFUSE: --confirm needs HL_PRIVATE_KEY in .env.")
        return 2
    if live and not s.trading_enabled:
        print("REFUSE: --confirm needs TRADING_ENABLED=true in .env.")
        return 2

    client = create_exchange_client(s)
    svc = OrderService(client, s, PreviewStore(), db=None)
    coin = (s.default_symbol or "BTC").split("_")[0]
    side = "long"

    print(f"Mode: {'LIVE (places real testnet orders)' if live else 'DRY-RUN (preview only)'}")
    print(f"Exchange: {client.base_url} testnet={client.testnet}  coin={coin}\n")

    try:
        snap = await client.account_snapshot()
        equity = float(snap.get("equity_usdt") or 0.0)
        print(f"Account equity: {equity:.2f} USDC  available: {snap.get('available_usdt')}")
        if live and equity < TARGET_NOTIONAL_USDT * 1.5:
            print(f"REFUSE: equity {equity} too low for a safe test.")
            await client.aclose()
            return 2

        meta = await client.contract_meta(coin)
        tk = await client.ticker(coin)
        last = float(tk.last_price)
        cs = float(meta.contract_size or 1.0)
        vol_unit = float(meta.vol_unit or 0.0)
        vol = _round_up_vol(TARGET_NOTIONAL_USDT / (cs * last), vol_unit, float(meta.min_vol or 0.0))
        lev = min(TEST_LEVERAGE, int(meta.max_leverage or TEST_LEVERAGE))
        notional = vol * cs * last
        print(
            f"Instrument: last={last} contract_size={cs} vol_unit={vol_unit} "
            f"maxLev={meta.max_leverage}\nTest size: vol={vol} (~{notional:.2f} USDC) lev={lev}\n"
        )

        # ── Test A: MARKET entry + SL, verify stop is really on exchange ──
        print("-- Test A: MARKET entry with protective SL --")
        a_ticket = OrderTicket(
            symbol=coin,
            side=side,
            order_type="market",
            vol=vol,
            leverage=lev,
            entry=last,
            stop_loss=_round_px(last * 0.98, meta.price_unit),
            take_profit=_round_px(last * 1.05, meta.price_unit),
            trigger_mode="auto",
            open_type=1,
        )
        prev = await svc.preview(a_ticket)
        rep.add("A: preview accepted", bool(prev.get("ok")), str(prev.get("errors") or ""))
        if live and prev.get("ok"):
            conf = await svc.confirm(prev["token"])
            status = conf.get("status")
            rep.add(
                "A: confirm placed",
                status in ("placed", "recovered_placed") and bool(conf.get("sl_verified")),
                f"status={status} sl_verified={conf.get('sl_verified')} detail={conf.get('sl_detail')}",
            )
            # THE key live check: is the SL trigger actually resting on HL?
            stops = await client.open_stop_orders(coin)
            has_stop = any(st.get("reduceOnly") is not False for st in stops)
            rep.add(
                "A: SL trigger present on exchange",
                bool(stops) and has_stop,
                f"{len(stops)} stop order(s): {[st.get('triggerPrice') for st in stops]}",
            )
            pos = await client.positions(coin)
            rep.add("A: position open on exchange", bool(pos), f"{len(pos or [])} position(s)")
        elif not live:
            rep.add("A: confirm placed", None, "dry-run")
            rep.add("A: SL trigger present on exchange", None, "dry-run")
            rep.add("A: position open on exchange", None, "dry-run")

        # Close A before Test B so the two don't interact.
        if live:
            try:
                pos = await client.positions(coin)
                if pos:
                    out = await svc.close_position(symbol=coin, side=side)
                    rep.add("A: close position", bool(out.get("ok")), f"status={out.get('status')}")
                    # cancel any leftover SL trigger now the position is gone
                    for oid in await _open_order_ids(client, coin):
                        try:
                            await client.cancel_order([{"orderId": oid, "symbol": coin}])
                        except Exception:  # noqa: BLE001
                            pass
            except Exception as e:  # noqa: BLE001
                rep.add("A: close position", False, f"{type(e).__name__}: {e}")

        # ── Test B: RESTING LIMIT (unfilled_resting fix) ──────────────────
        print("\n-- Test B: non-marketable RESTING LIMIT (unfilled_resting fix) --")
        limit_px = _round_px(last * 0.97, meta.price_unit)  # buy 3% below market → rests
        b_ticket = OrderTicket(
            symbol=coin,
            side=side,
            order_type="limit",
            vol=vol,
            leverage=lev,
            price=limit_px,
            entry=limit_px,
            stop_loss=_round_px(limit_px * 0.98, meta.price_unit),
            take_profit=_round_px(limit_px * 1.05, meta.price_unit),
            trigger_mode="auto",
            open_type=1,
        )
        prevb = await svc.preview(b_ticket)
        rep.add("B: preview accepted", bool(prevb.get("ok")), str(prevb.get("errors") or ""))
        if live and prevb.get("ok"):
            confb = await svc.confirm(prevb["token"])
            status = confb.get("status")
            warns = confb.get("warnings") or []
            rep.add(
                "B: rests as placed_unfilled_resting",
                status == "placed_unfilled_resting",
                f"status={status}",
            )
            rep.add(
                "B: not falsely flattened/cancelled",
                confb.get("flatten") is None,
                f"flatten={confb.get('flatten')}",
            )
            rep.add(
                "B: 'LIMIT RUHT' warning present",
                any("LIMIT RUHT" in w for w in warns),
                "",
            )
            resting = await _open_order_ids(client, coin)
            rep.add("B: limit is actually resting on exchange", bool(resting), f"orders={resting}")
        elif not live:
            for n in (
                "B: rests as placed_unfilled_resting",
                "B: not falsely flattened/cancelled",
                "B: 'LIMIT RUHT' warning present",
                "B: limit is actually resting on exchange",
            ):
                rep.add(n, None, "dry-run")

        if live:
            await _cleanup(client, svc, coin, side, rep)

    except (HyperliquidError, OrderError) as e:
        rep.add("exchange/order error", False, f"{type(e).__name__}: {e}")
        if live:
            await _cleanup(client, svc, coin, side, rep)
    finally:
        rep.summary()
        await client.aclose()

    if not live:
        print("\nDry-run only -- no orders placed. Re-run with --confirm to test live on TESTNET.")
    return 1 if rep.failed else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Actually place/close orders on the TESTNET (default: dry-run preview only)",
    )
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args.confirm)))

#!/usr/bin/env python3
"""Manual Hyperliquid TESTNET order-lifecycle smoke test.

Exercises the real OrderService -> HyperliquidClient path against Hyperliquid
Testnet:

  A) MARKET entry with protective SL -> verify the position and the concrete
     exchange-side stop.
  B) LIMIT entry safety gate -> verify that the app still refuses Hyperliquid
     limit entries until a persistent fill watcher exists.

Live mode first acquires the app instance lock and refuses to mutate an account
that already has a position or open order for the selected coin. Cleanup closes
only the position created after that clean baseline and cancels only order IDs
returned by this run. Do not use the same Testnet account manually while the
probe is running.

Usage (from repo root, venv python):
  .venv/Scripts/python.exe scripts/hl_smoke_trade.py
  .venv/Scripts/python.exe scripts/hl_smoke_trade.py --with-account
  .venv/Scripts/python.exe scripts/hl_smoke_trade.py --confirm TESTNET

Safety:
  * Hard-refuses unless exchange=hyperliquid and HL_TESTNET=true.
  * Default mode passes no wallet credentials and performs no private reads.
  * --with-account needs a valid Hyperliquid account identity but never confirms.
  * --confirm TESTNET needs that same validated identity and TRADING_ENABLED=true.
  * Targets ~$20 notional at low leverage and hard-refuses above $50 after
    exchange lot-size rounding.
  * Never exercises a Hyperliquid limit entry.
  * Live entry and cleanup mutations require the local audit DB.

Faucet / UI: https://app.hyperliquid-testnet.xyz
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import math
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db.repo import Database  # noqa: E402
from app.exchange_factory import create_exchange_client  # noqa: E402
from app.hyperliquid.client import _required_hl_coin  # noqa: E402
from app.hyperliquid.errors import HyperliquidError  # noqa: E402
from app.models import OrderTicket  # noqa: E402
from app.orders.service import OrderService  # noqa: E402
from app.orders.tokens import PreviewStore  # noqa: E402

TARGET_NOTIONAL_USDT = 20.0
MAX_TEST_NOTIONAL_USDT = 50.0
TEST_LEVERAGE = 3
HL_TESTNET_HOST = "api.hyperliquid-testnet.xyz"
TESTNET_CONFIRMATION = "TESTNET"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, ok: bool | None, detail: str = "") -> None:
        tag = "PASS" if ok else ("SKIP" if ok is None else "FAIL")
        self.rows.append((tag, name, detail))
        print(f"  [{tag}] {name}" + (f" - {detail}" if detail else ""))

    @property
    def failed(self) -> bool:
        return any(tag == "FAIL" for tag, _, _ in self.rows)

    def summary(self) -> None:
        print("\n" + "=" * 64)
        for tag, name, _ in self.rows:
            print(f"  {tag:<4} {name}")
        n_fail = sum(1 for tag, _, _ in self.rows if tag == "FAIL")
        n_pass = sum(1 for tag, _, _ in self.rows if tag == "PASS")
        n_skip = sum(1 for tag, _, _ in self.rows if tag == "SKIP")
        print("=" * 64)
        print(f"  {n_pass} passed, {n_fail} failed, {n_skip} skipped")


def _safe_exception_detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}; provider details suppressed"


def _testnet_client_error(client: Any) -> str | None:
    """Validate the constructed client before any exchange request."""
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


def _acquire_live_probe_lock(database_path: str):
    """Share the app's process lock for the money-mutating probe path."""
    from app.main import _acquire_instance_lock

    return _acquire_instance_lock(Database(database_path).path.parent)


def _release_live_probe_lock(lock_path) -> None:
    from app.main import _release_instance_lock

    _release_instance_lock(lock_path)


def _round_px(px: float, unit: float) -> float:
    return round(px / unit) * unit if unit and unit > 0 else px


def _round_up_vol(raw: float, unit: float, min_vol: float) -> float:
    volume = math.ceil(raw / unit) * unit if unit and unit > 0 else raw
    return max(volume, min_vol or 0.0)


def _require_market_identity(meta: Any, ticker: Any, coin: str) -> None:
    """Bind every diagnostic market-data object to the requested HL coin."""
    expected = coin.strip().upper()
    for source, value in (
        ("contract metadata", getattr(meta, "symbol", None)),
        ("ticker", getattr(ticker, "symbol", None)),
    ):
        if type(value) is not str or value.strip().upper() != expected:
            raise HyperliquidError(f"{source} symbol does not match the probe coin")


def _account_balances(snapshot: Any) -> tuple[float, float]:
    """Validate the private snapshot before using or displaying its balances."""
    if not isinstance(snapshot, dict):
        raise HyperliquidError("account snapshot has an invalid shape")
    balances: list[float] = []
    for field in ("equity_usdt", "available_usdt"):
        raw = snapshot.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise HyperliquidError("account snapshot contains invalid balances")
        try:
            value = float(raw)
        except OverflowError as exc:
            raise HyperliquidError(
                "account snapshot contains invalid balances"
            ) from exc
        if not math.isfinite(value) or value < 0:
            raise HyperliquidError("account snapshot contains invalid balances")
        balances.append(value)
    return balances[0], balances[1]


def _positive_order_id(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean order id")
    try:
        order_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid order id") from exc
    if order_id <= 0 or str(value).strip() != str(order_id):
        raise ValueError("invalid order id")
    return order_id


def _order_ids(rows: Any, *, source: str) -> set[int]:
    if not isinstance(rows, list):
        raise HyperliquidError(f"{source} returned an unrecognized shape")
    result: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise HyperliquidError(f"{source} contains a non-object row")
        try:
            order_id = _positive_order_id(row.get("orderId"))
        except ValueError as exc:
            raise HyperliquidError(f"{source} contains an invalid order id") from exc
        if order_id in result:
            raise HyperliquidError(f"{source} contains a duplicate order id")
        result.add(order_id)
    return result


async def _open_order_ids(client, coin: str) -> set[int]:
    """Fetch ordinary and trigger IDs, failing closed on either exchange read."""
    stops = _order_ids(await client.open_stop_orders(coin), source="open_stop_orders")
    ordinary = _order_ids(await client.open_orders(coin), source="open_orders")
    if stops & ordinary:
        raise HyperliquidError("open order sources contain overlapping identities")
    return stops | ordinary


async def _position_abs(client, coin: str) -> float:
    # Baseline and cleanup decisions must bypass the short account cache. An
    # external Testnet actor does not invalidate this process's cached state;
    # stale data could otherwise close the wrong remaining size or remove
    # protection while exposure still exists.
    rows = await client.positions(coin, fresh=True)
    if not isinstance(rows, list):
        raise HyperliquidError("positions returned an unrecognized shape")
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            raise HyperliquidError("positions contains a non-object row")
        raw = row.get("holdVol")
        if raw is None:
            raw = row.get("positionAmt")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise HyperliquidError("positions contains an invalid volume")
        try:
            volume = float(raw)
        except OverflowError as exc:
            raise HyperliquidError("positions contains an invalid volume") from exc
        if not math.isfinite(volume):
            raise HyperliquidError("positions contains a non-finite volume")
        total += abs(volume)
    return total


def _created_order_ids(confirm_result: Any) -> set[int]:
    if not isinstance(confirm_result, dict):
        return set()
    response = confirm_result.get("response")
    if not isinstance(response, dict):
        return set()
    result: set[int] = set()
    for key in ("orderId", "slTriggerOid", "tpTriggerOid", "tpTriggerOid2"):
        value = response.get(key)
        if value is None:
            continue
        try:
            order_id = _positive_order_id(value)
        except ValueError as exc:
            raise HyperliquidError(
                f"confirm response contains invalid {key}"
            ) from exc
        if order_id in result:
            raise HyperliquidError("confirm response contains overlapping order identities")
        result.add(order_id)
    return result


_CONFIRMED_ENTRY_PLACEMENT_STATUSES = frozenset(
    {
        "placed",
        "recovered_placed",
        "placed_unfilled_resting",
        "recovered_placed_unfilled_resting",
        "placed_sl_unknown",
        "recovered_placed_sl_unknown",
        "placed_sl_unverified",
        "recovered_placed_sl_unverified",
        "placed_sl_unverified_flatten_sent",
        "recovered_placed_sl_unverified_flatten_sent",
        "placed_partial_fill",
        "recovered_placed_partial_fill",
        "placed_coverage_unknown",
        "recovered_placed_coverage_unknown",
    }
)
_CONFIRMED_UNFILLED_ENTRY_STATUSES = frozenset(
    {"placed_unfilled_resting", "recovered_placed_unfilled_resting"}
)


def _confirm_proves_entry_placement(confirm_result: Any) -> bool:
    if not isinstance(confirm_result, dict):
        return False
    if confirm_result.get("ok") is not True:
        return False
    status = confirm_result.get("status")
    return isinstance(status, str) and status in _CONFIRMED_ENTRY_PLACEMENT_STATUSES


def _confirm_proves_entry_fill(confirm_result: Any) -> bool:
    return (
        _confirm_proves_entry_placement(confirm_result)
        and confirm_result.get("status") not in _CONFIRMED_UNFILLED_ENTRY_STATUSES
    )


async def _cleanup(
    client,
    service: OrderService,
    coin: str,
    side: str,
    report: Report,
    *,
    position_may_be_owned: bool,
    owned_position_vol: float | None = None,
    owned_order_ids: set[int],
) -> None:
    print("\n-- Cleanup --")
    cancellation: asyncio.CancelledError | None = None

    if position_may_be_owned:
        try:
            if (
                owned_position_vol is None
                or not math.isfinite(owned_position_vol)
                or owned_position_vol <= 0
            ):
                raise HyperliquidError(
                    "probe-owned position volume is unavailable; close blocked"
                )
            volume = await _position_abs(client, coin)
            if volume > 1e-9:
                # The instance lock excludes this app, but another device or API
                # actor can still add same-coin exposure. Never let diagnostic
                # cleanup close more than the probe itself requested or more than
                # the position that remains after an external partial reduction.
                close_volume = min(volume, owned_position_vol)
                result = await service.close_position(
                    symbol=coin,
                    side=side,
                    vol=close_volume,
                )
                report.add(
                    "cleanup: close created position",
                    bool(result.get("ok")) and bool(result.get("verified")),
                    f"status={result.get('status')}",
                )
            else:
                report.add("cleanup: created position already flat", True)
        except asyncio.CancelledError as exc:
            cancellation = exc
            report.add(
                "cleanup: close created position",
                False,
                _safe_exception_detail(exc),
            )
        except Exception as exc:  # noqa: BLE001 - cleanup must continue to orders
            report.add(
                "cleanup: close created position",
                False,
                _safe_exception_detail(exc),
            )
    else:
        report.add("cleanup: no confirmed probe-owned position", True)

    position_after_close: float | None = None
    try:
        # A failed, cancelled or partially-filled close can leave the position
        # open. Never remove its probe-owned SL/TP orders until a fresh account
        # read proves the selected coin is flat; otherwise cleanup itself would
        # turn a protected residual into an unprotected one.
        if owned_order_ids:
            position_after_close = await _position_abs(client, coin)
        if (
            position_after_close is not None
            and position_after_close > 1e-9
            and position_may_be_owned
        ):
            report.add(
                "cleanup: cancel created orders",
                False,
                (
                    f"position={position_after_close:g} remains; probe-owned "
                    "protective orders kept"
                ),
            )
        else:
            open_ids = await _open_order_ids(client, coin)
            pending_owned = open_ids & owned_order_ids
            cancelled: list[int] = []
            for order_id in sorted(pending_owned):
                await service.cancel(order_id=order_id, symbol=coin)
                cancelled.append(order_id)
            report.add(
                "cleanup: cancel created orders",
                True,
                f"cancelled={cancelled or 'none'}",
            )
    except asyncio.CancelledError as exc:
        cancellation = cancellation or exc
        report.add(
            "cleanup: cancel created orders",
            False,
            _safe_exception_detail(exc),
        )
    except Exception as exc:  # noqa: BLE001 - report cleanup failure explicitly
        report.add(
            "cleanup: cancel created orders",
            False,
            _safe_exception_detail(exc),
        )

    try:
        # Prove the final state after order cancellation. Reusing the earlier
        # pre-cancellation value could report a clean probe after an external
        # actor changed the selected-coin position in the meantime.
        leftover_position = await _position_abs(client, coin)
        leftover_ids = await _open_order_ids(client, coin)
        leftover_owned = leftover_ids & owned_order_ids
        clean = leftover_position <= 1e-9 and not leftover_owned
        report.add(
            "cleanup: created exposure removed",
            clean,
            (
                f"position={leftover_position:g} "
                f"owned_open_orders={sorted(leftover_owned)}"
            ),
        )
        unrelated = leftover_ids - owned_order_ids
        if unrelated:
            report.add(
                "cleanup: unrelated orders preserved",
                False,
                f"unexpected orders appeared during probe: {sorted(unrelated)}",
            )
    except asyncio.CancelledError as exc:
        cancellation = cancellation or exc
        report.add("cleanup: verify", False, _safe_exception_detail(exc))
    except Exception as exc:  # noqa: BLE001 - report cleanup failure explicitly
        report.add("cleanup: verify", False, _safe_exception_detail(exc))

    if cancellation is not None:
        raise cancellation


async def run(live: bool, *, with_account: bool = False) -> int:
    if live and with_account:
        print("REFUSE: choose either --with-account or --confirm TESTNET, not both.")
        return 2
    settings = get_settings()
    report = Report()
    account_access = live or with_account

    if (settings.exchange or "").lower() not in ("hyperliquid", "hl"):
        print(f"REFUSE: exchange is '{settings.exchange}', not hyperliquid.")
        return 2
    if not settings.hl_testnet:
        print("REFUSE: HL_TESTNET is false -- this script never runs on Mainnet.")
        return 2
    if account_access and not settings.hl_ready:
        mode = "--confirm TESTNET" if live else "--with-account"
        print(
            f"REFUSE: {mode} needs a valid HL_PRIVATE_KEY and, when set, "
            "a valid HL_ACCOUNT_ADDRESS."
        )
        return 2
    if live and not settings.trading_enabled:
        print("REFUSE: --confirm TESTNET needs TRADING_ENABLED=true.")
        return 2
    try:
        coin = _required_hl_coin(settings.default_symbol, field="probe")
    except HyperliquidError:
        print("REFUSE: DEFAULT_SYMBOL is invalid for Hyperliquid.")
        return 2

    instance_lock = None
    if live:
        try:
            instance_lock = _acquire_live_probe_lock(settings.database_path)
        except OSError as exc:
            print(
                "REFUSE: cannot acquire the app instance lock: "
                f"{_safe_exception_detail(exc)}"
            )
            return 2
        if instance_lock is None:
            print("REFUSE: another app/probe process owns the instance lock.")
            return 2

    client = None
    service = None
    audit_db = None
    baseline_clean = False
    position_may_be_owned = False
    owned_position_vol: float | None = None
    owned_order_ids: set[int] = set()
    side = "long"

    try:
        client_settings = settings
        if not account_access:
            client_settings = copy.copy(settings)
            client_settings.hl_private_key = ""
            client_settings.hl_account_address = ""
        client = create_exchange_client(client_settings)
        client_error = _testnet_client_error(client)
        if client_error:
            report.add("client targets Hyperliquid Testnet", False, client_error)
            return 2
        report.add(
            "client targets Hyperliquid Testnet",
            True,
            f"host={HL_TESTNET_HOST}",
        )
        print(
            "Mode: "
            + (
                "LIVE (places real Testnet orders)"
                if live
                else "ACCOUNT PREVIEW (no mutations)"
                if with_account
                else "PUBLIC PREFLIGHT (no credentials or private reads)"
            )
        )
        print(f"Exchange: {client.base_url} testnet={client.testnet}  coin={coin}\n")

        if not account_access:
            meta = await client.contract_meta(coin)
            ticker = await client.ticker(coin)
            _require_market_identity(meta, ticker, coin)
            last = float(ticker.last_price)
            if not math.isfinite(last) or last <= 0:
                raise HyperliquidError("ticker contains an invalid last price")
            report.add(
                "public Testnet market preflight",
                bool(meta) and bool(ticker),
                f"symbol={coin} last={last:g}",
            )
            print("\nPublic preflight only -- no account read or order preview performed.")
            return 1 if report.failed else 0

        snapshot = await client.account_snapshot()
        equity, available = _account_balances(snapshot)
        print(
            f"Account equity: {equity:.2f} USDC  "
            f"available: {available:.2f}"
        )
        if live and equity < TARGET_NOTIONAL_USDT * 1.5:
            print(f"REFUSE: equity {equity} too low for a safe test.")
            return 2

        if live:
            initial_position = await _position_abs(client, coin)
            initial_orders = await _open_order_ids(client, coin)
            baseline_clean = initial_position <= 1e-9 and not initial_orders
            report.add(
                "live baseline has no selected-coin exposure",
                baseline_clean,
                f"position={initial_position:g} open_orders={sorted(initial_orders)}",
            )
            if not baseline_clean:
                print(
                    "REFUSE: preserve the existing position/orders; choose a clean "
                    "Testnet account or clear them manually after review."
                )
                return 2

            # A live diagnostic still uses the production money path, so its
            # entry and cleanup outcomes need the same durable order audit. The
            # DB is opened only after the clean-baseline gate and before Preview.
            audit_db = Database(settings.database_path)
            await audit_db.init()
            await audit_db.open()

        meta = await client.contract_meta(coin)
        ticker = await client.ticker(coin)
        _require_market_identity(meta, ticker, coin)
        sizing_values = (
            ticker.last_price,
            meta.contract_size,
            meta.vol_unit,
            meta.min_vol,
            meta.price_unit,
        )
        if any(isinstance(value, bool) for value in sizing_values):
            raise HyperliquidError("Testnet sizing metadata is invalid")
        try:
            last = float(ticker.last_price)
            contract_size = float(meta.contract_size)
            volume_unit = float(meta.vol_unit)
            min_volume = float(meta.min_vol)
            price_unit = float(meta.price_unit)
        except (TypeError, ValueError, OverflowError) as exc:
            raise HyperliquidError("Testnet sizing metadata is invalid") from exc
        if (
            not all(
                math.isfinite(value)
                for value in (last, contract_size, volume_unit, min_volume, price_unit)
            )
            or last <= 0
            or contract_size <= 0
            or volume_unit <= 0
            or min_volume <= 0
            or price_unit < 0
        ):
            raise HyperliquidError("Testnet sizing metadata is invalid")
        max_leverage = meta.max_leverage
        if (
            isinstance(max_leverage, bool)
            or not isinstance(max_leverage, int)
            or max_leverage <= 0
        ):
            raise HyperliquidError("Testnet sizing metadata is invalid")
        volume = _round_up_vol(
            TARGET_NOTIONAL_USDT / (contract_size * last),
            volume_unit,
            min_volume,
        )
        leverage = min(TEST_LEVERAGE, max_leverage)
        notional = volume * contract_size * last
        if not math.isfinite(volume) or volume <= 0 or not math.isfinite(notional):
            raise HyperliquidError("Testnet sizing result is invalid")
        if notional > MAX_TEST_NOTIONAL_USDT + 1e-9:
            report.add(
                "Testnet probe notional stays within its hard limit",
                False,
                (
                    f"rounded notional={notional:.2f} USDC exceeds "
                    f"{MAX_TEST_NOTIONAL_USDT:.2f} USDC"
                ),
            )
            print(
                "REFUSE: exchange lot-size rounding would exceed the Testnet "
                f"probe hard limit of {MAX_TEST_NOTIONAL_USDT:.2f} USDC."
            )
            return 2
        service = OrderService(client, settings, PreviewStore(), db=audit_db)
        print(
            f"Instrument: last={last} contract_size={contract_size} "
            f"vol_unit={volume_unit} maxLev={meta.max_leverage}\n"
            f"Test size: vol={volume} (~{notional:.2f} USDC) lev={leverage}\n"
        )

        print("-- Test A: MARKET entry with protective SL --")
        market_ticket = OrderTicket(
            symbol=coin,
            side=side,
            order_type="market",
            vol=volume,
            leverage=leverage,
            entry=last,
            stop_loss=_round_px(last * 0.98, price_unit),
            take_profit=_round_px(last * 1.05, price_unit),
            trigger_mode="auto",
            open_type=1,
        )
        preview = await service.preview(market_ticket)
        report.add(
            "A: preview accepted",
            bool(preview.get("ok")),
            str(preview.get("errors") or ""),
        )
        if live and preview.get("ok"):
            preconfirm_position = await _position_abs(client, coin)
            preconfirm_orders = await _open_order_ids(client, coin)
            preconfirm_clean = (
                preconfirm_position <= 1e-9 and not preconfirm_orders
            )
            report.add(
                "A: pre-confirm baseline remains clean",
                preconfirm_clean,
                (
                    f"position={preconfirm_position:g} "
                    f"open_orders={sorted(preconfirm_orders)}"
                ),
            )
            if not preconfirm_clean:
                raise HyperliquidError(
                    "selected-coin exposure changed after preview; confirm blocked"
                )
            confirmed = await service.confirm(preview["token"])
            # Response-bound IDs are safe cleanup targets even when the status
            # is malformed or proves only an unfilled resting order.
            owned_order_ids.update(_created_order_ids(confirmed))
            if not _confirm_proves_entry_placement(confirmed):
                raise HyperliquidError(
                    "confirm response does not prove Testnet entry placement"
                )
            # An accepted-but-unfilled order proves ownership of the order ID,
            # not of any position concurrently observed on the same coin.
            position_may_be_owned = _confirm_proves_entry_fill(confirmed)
            owned_position_vol = volume if position_may_be_owned else None
            status = confirmed.get("status")
            report.add(
                "A: confirm placed",
                status in ("placed", "recovered_placed")
                and bool(confirmed.get("sl_verified")),
                (
                    f"status={status} sl_verified={confirmed.get('sl_verified')} "
                    f"detail={confirmed.get('sl_detail')}"
                ),
            )

            stops = await client.open_stop_orders(coin)
            stop_ids = _order_ids(stops, source="open_stop_orders")
            # Observation proves whether our exact response-bound SL is open;
            # it does not establish ownership of every stop on the coin. A
            # second device can create one after the clean baseline.
            response = confirmed.get("response")
            expected_sl = response.get("slTriggerOid") if isinstance(response, dict) else None
            expected_sl_id = (
                _positive_order_id(expected_sl) if expected_sl is not None else None
            )
            has_expected_stop = expected_sl_id is not None and expected_sl_id in stop_ids
            report.add(
                "A: own SL trigger present on exchange",
                has_expected_stop,
                f"expected={expected_sl_id} observed={sorted(stop_ids)}",
            )
            open_volume = await _position_abs(client, coin)
            report.add(
                "A: position open on exchange",
                open_volume > 1e-9,
                f"volume={open_volume:g}",
            )
        elif not live:
            report.add("A: confirm placed", None, "dry-run")
            report.add("A: own SL trigger present on exchange", None, "dry-run")
            report.add("A: position open on exchange", None, "dry-run")

        print("\n-- Test B: Hyperliquid LIMIT safety gate --")
        limit_price = _round_px(last * 0.97, price_unit)
        limit_ticket = OrderTicket(
            symbol=coin,
            side=side,
            order_type="limit",
            vol=volume,
            leverage=leverage,
            price=limit_price,
            entry=limit_price,
            stop_loss=_round_px(limit_price * 0.98, price_unit),
            take_profit=_round_px(limit_price * 1.05, price_unit),
            trigger_mode="auto",
            open_type=1,
        )
        limit_preview = await service.preview(limit_ticket)
        limit_errors = [str(error) for error in (limit_preview.get("errors") or [])]
        limit_blocked = not limit_preview.get("ok") and any(
            "Hyperliquid limit entries are disabled" in error
            for error in limit_errors
        )
        report.add(
            "B: limit entry remains blocked",
            limit_blocked,
            str(limit_errors),
        )

    except Exception as exc:  # noqa: BLE001 - CLI must not print raw tracebacks
        report.add("probe error", False, _safe_exception_detail(exc))
    finally:
        cleanup_cancellation: asyncio.CancelledError | None = None
        if live and baseline_clean and client is not None and service is not None:
            try:
                await _cleanup(
                    client,
                    service,
                    coin,
                    side,
                    report,
                    position_may_be_owned=position_may_be_owned,
                    owned_position_vol=owned_position_vol,
                    owned_order_ids=owned_order_ids,
                )
            except asyncio.CancelledError as exc:
                cleanup_cancellation = exc
        if client is not None:
            try:
                await client.aclose()
            except asyncio.CancelledError as exc:
                cleanup_cancellation = cleanup_cancellation or exc
                report.add(
                    "cleanup: close exchange client",
                    False,
                    _safe_exception_detail(exc),
                )
            except Exception as exc:  # noqa: BLE001 - keep final report secret-free
                report.add(
                    "cleanup: close exchange client",
                    False,
                    _safe_exception_detail(exc),
                )
        if audit_db is not None:
            try:
                await audit_db.close()
            except asyncio.CancelledError as exc:
                cleanup_cancellation = cleanup_cancellation or exc
                report.add(
                    "cleanup: close audit database",
                    False,
                    _safe_exception_detail(exc),
                )
            except Exception as exc:  # noqa: BLE001 - keep final report secret-free
                report.add(
                    "cleanup: close audit database",
                    False,
                    _safe_exception_detail(exc),
                )
        if instance_lock is not None:
            try:
                _release_live_probe_lock(instance_lock)
            except Exception as exc:  # noqa: BLE001 - report without local paths
                report.add(
                    "cleanup: release instance lock",
                    False,
                    _safe_exception_detail(exc),
                )
        report.summary()
        if cleanup_cancellation is not None:
            raise cleanup_cancellation

    if not live:
        print(
            "\nAccount preview only -- no orders placed. Re-run with "
            "--confirm TESTNET to mutate TESTNET."
        )
    return 1 if report.failed else 0


def _parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--with-account",
        action="store_true",
        help="Run private account-backed Preview checks without placing orders",
    )
    mode.add_argument(
        "--confirm",
        choices=(TESTNET_CONFIRMATION,),
        metavar="TESTNET",
        help=(
            "Actually place/close orders on TESTNET; the exact TESTNET value is "
            "required (default: public preflight)"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    arguments = _parse_arguments()
    raise SystemExit(
        asyncio.run(
            run(
                arguments.confirm == TESTNET_CONFIRMATION,
                with_account=arguments.with_account,
            )
        )
    )

"""Hyperliquid (testnet/mainnet) adapter with MEXC-like surface for OrderService.

Size = coin amount (e.g. 0.001 BTC). contract_size is always 1.0 so risk =
vol * abs(entry - stop) in USDC terms for linear perps.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from app.hyperliquid.errors import HyperliquidError
from app.models import Candle, ContractMeta, FundingRate, Ticker

INTERVAL_MAP = {
    "5m": "5m",
    "15m": "15m",
    "1H": "1h",
    "4H": "4h",
    "1D": "1d",
}


def _opt_f(v: Any) -> float | None:
    """float(v) or None — for optional numeric ctx fields (openInterest, premium)."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        value = float(v)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _required_finite_float(v: Any, field: str) -> float:
    """Parse a required exchange number or fail with adapter semantics."""
    if isinstance(v, bool):
        raise HyperliquidError(f"Invalid {field}")
    try:
        value = float(v)
    except (TypeError, ValueError) as exc:
        raise HyperliquidError(f"Invalid {field}") from exc
    if not math.isfinite(value):
        raise HyperliquidError(f"Non-finite {field}")
    return value


def _required_int(v: Any, field: str, *, minimum: int) -> int:
    if isinstance(v, bool):
        raise HyperliquidError(f"{field} is missing or invalid")
    if isinstance(v, int):
        value = v
    elif isinstance(v, str) and v.isdigit():
        value = int(v)
    else:
        raise HyperliquidError(f"{field} is missing or invalid")
    if value < minimum:
        raise HyperliquidError(f"{field} must be >= {minimum}")
    return value


def to_hl_coin(symbol: str) -> str:
    """BTC_USDT / BTC-USDT / BTC → BTC."""
    s = (symbol or "").strip().upper().replace("-", "_")
    if "_" in s:
        s = s.split("_")[0]
    return s


# Hyperliquid perp min order value (USDC notional)
HL_MIN_NOTIONAL_USD = 10.0

# TTL for the perp universe/meta cache (tick + leverage rules, listed coins).
# Was cached forever, so tick/leverage-rule updates and newly-listed coins never
# refreshed within a session (audit exchange M-1). 1h keeps upstream load low.
_META_TTL_S = 3600.0

log = logging.getLogger("app.hyperliquid.client")

# Short-TTL cache for user_state (clearinghouseState). assets() and positions()
# each hit info.user_state independently, so every account_snapshot cost 2 calls;
# combined with frontend polls (/api/account, /api/orders/open, /api/market,
# /api/fills) and the trade-monitor loop this drove Hyperliquid 429s → /api/market
# 502 + "trade monitor: account_snapshot failed". A 2s TTL collapses the 2 reads
# per snapshot into 1 and dedups rapid concurrent polls.
_USER_STATE_TTL_S = 2.0
# On a fetch error (esp. a 429 ClientError) serve the last good state for up to
# this age instead of raising — rides out a transient 429 burst for the
# read/display/monitor paths. Equity/positions don't meaningfully move in 8s;
# beyond it we fail honestly rather than trust arbitrarily stale money data.
_USER_STATE_MAX_STALE_S = 8.0

# Transient-429 retry for READ/data paths only (see _to_thread). A market scan
# (candle/meta fan-out over the whole universe) or a boot burst briefly trips
# Hyperliquid's CloudFront 429; a short exponential backoff + retry rides it out
# instead of surfacing a 502. NEVER applied to the money path (place/modify/
# cancel): a 429 there might follow a send that actually landed, so a silent
# resend could double the order — those fail honestly.
_RATE_LIMIT_RETRIES = 2
# 0.5s, 1.0s (exp) → ~1.5s worst-case. Kept short on purpose: a longer retry
# chain on a monitor read fan-out under sustained 429 would push the trade-monitor
# cycle past its interval and delay auto-BE/trailing.
_RATE_LIMIT_BACKOFF_S = 0.5
# Hard ceiling on a single token-bucket wait so a fat-fingered tiny hl_read_max_rps
# (e.g. a per-minute/per-second mix-up) can never stall the read path for minutes.
_MAX_ACQUIRE_WAIT_S = 5.0


def _is_rate_limited(e: Exception) -> bool:
    """True iff the SDK exception is a Hyperliquid 429 (rate limit).

    Primary signal is the SDK's ClientError.status_code (always set to 429 on a
    real rate-limit); the text fallback matches the standard reason phrase rather
    than the bare digits "429" (which could appear in a price/id and cause a
    spurious retry on an unrelated error)."""
    if getattr(e, "status_code", None) == 429:
        return True
    return "too many requests" in str(e).lower()


class _AsyncTokenBucket:
    """Async token bucket that paces HL READ calls under the per-IP rate limit.

    Refills at ``rate`` tokens/sec up to ``capacity``. ``acquire()`` returns
    immediately while tokens remain (a short burst passes free) and otherwise
    sleeps just long enough for one token to accrue — turning a scanner klines
    flood (up to ~universe_size×3 calls) into a paced stream instead of a 429
    burst. The lock is intentionally held across the sleep so waiters form an
    ordered, evenly-spaced queue at the target rate.

    NEVER used for the money path (place/modify/cancel): an order must not wait
    behind a scanner fan-out. See HyperliquidClient._to_thread.
    """

    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = max(1e-6, float(rate))
        self._capacity = max(1.0, float(capacity))
        self._tokens = self._capacity
        self._last: float | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._last is None:
                self._last = now
            self._tokens = min(
                self._capacity, self._tokens + (now - self._last) * self._rate
            )
            self._last = now
            if self._tokens < 1.0:
                wait = min(_MAX_ACQUIRE_WAIT_S, (1.0 - self._tokens) / self._rate)
                await asyncio.sleep(wait)
                self._tokens = 0.0
                self._last = time.monotonic()
            else:
                self._tokens -= 1.0


def round_hl_price(px: float, sz_decimals: int) -> float:
    """Round price to Hyperliquid tick rules.

    Perp prices: max 5 significant figures AND max (6 - szDecimals) decimals.
    Integer prices are always allowed regardless of significant figures.
    """
    if px is None or px <= 0:
        return px
    max_dec = max(0, 6 - int(sz_decimals or 0))
    if px >= 100_000:
        # 6+ integer digits: integer prices always allowed
        return float(round(px))
    rounded_sig = float(f"{px:.5g}")
    return round(rounded_sig, max_dec)


def round_hl_price_side_aware(
    px: float, sz_decimals: int, *, is_buy: bool, kind: str = "sl"
) -> float:
    """Side-aware variant of ``round_hl_price`` for SL/TP trigger prices (X2-05).

    Same precision grid as ``round_hl_price`` (5 significant figures AND max
    ``6 - szDecimals`` decimals), but rounds DIRECTIONALLY toward entry instead
    of nearest, so the exchange-tick precision cut can never make the placed
    trigger RISKIER than the value the risk gate approved:

      SL  is_buy True  (long, SL below entry)  -> ceil  (up, toward entry)
          is_buy False (short, SL above entry) -> floor (down, toward entry)
      TP  is_buy True  (long, TP above entry)  -> floor (down, toward entry)
          is_buy False (short, TP below entry) -> ceil  (up, toward entry)

    Why toward entry (reverses the prior round-15 "away from entry" choice):
    on Hyperliquid ``price_unit == 0``, so the gate's ``round_trigger_to_unit``
    is a no-op and computes RRR/realized risk on the RAW SL/TP. If the client
    then rounded AWAY from entry it would widen the SL below (or above) that raw
    value, so the placed loss would exceed the gate-approved risk by up to one
    tick. Rounding TOWARD entry keeps realized risk ≤ gate and RRR ≤ gate
    (reward never overstated). Uses Decimal so the grid step (a power of ten)
    divides exactly, avoiding the float dust nearest-rounding's ``round()`` has
    to shrug off.
    """
    if px is None or px <= 0:
        return px
    max_dec = max(0, 6 - int(sz_decimals or 0))
    k = (kind or "sl").strip().lower()
    is_sl = k in ("sl", "stop", "stop_loss", "stoploss")
    if is_sl:
        rounding = ROUND_CEILING if is_buy else ROUND_FLOOR
    else:  # take-profit
        rounding = ROUND_FLOOR if is_buy else ROUND_CEILING
    d = Decimal(str(px))
    if px >= 100_000:
        # 6+ integer digits: integer prices always allowed (mirrors round_hl_price).
        return float(d.to_integral_value(rounding=rounding))
    exp = d.adjusted()  # floor(log10(px)) for a normalized Decimal
    sig_step = Decimal(1).scaleb(exp - 4)  # 10^(exp-4): 5-sig-fig grid
    dec_step = Decimal(1).scaleb(-max_dec)  # 10^-max_dec: max-decimals grid
    step = max(sig_step, dec_step)  # coarser of the two wins, same as chained round_hl_price
    steps = (d / step).to_integral_value(rounding=rounding)
    return float(steps * step)


def external_oid_to_cloid(external_oid: str):
    """Map our ``mlt-…`` externalOid to a valid Hyperliquid Cloid.

    HL Cloid = "0x" + 32 hex chars (16 bytes). Our externalOid is not valid
    hex, so derive a deterministic 128-bit id via SHA-256. Deterministic means
    timeout-recovery can recompute the SAME cloid from the same externalOid and
    look the order up by it.
    """
    from hyperliquid.utils.types import Cloid

    oid = (external_oid or "").strip()
    if not oid:
        return None
    digest = hashlib.sha256(oid.encode("utf-8")).hexdigest()[:32]
    return Cloid.from_str("0x" + digest)


class HyperliquidClient:
    exchange_id = "hyperliquid"

    def __init__(
        self,
        *,
        private_key: str = "",
        account_address: str = "",
        testnet: bool = True,
        base_url: str | None = None,
        market_slippage_pct: float = 0.5,
        http_timeout_s: float = 10.0,
    ):
        from hyperliquid.utils import constants

        self.testnet = testnet
        # Q-01: hard upstream timeout for every SDK HTTP call. asyncio.wait_for
        # around to_thread does NOT abort a blocked requests call, so the only
        # real stall bound is a request-level timeout on the SDK's session.
        self._http_timeout_s = float(http_timeout_s)
        # Q-01: SEPARATED, size-bounded executors. A single bounded pool would
        # still let a scanner fan-out of hanging HL calls fill it and starve
        # confirm/close. So the money path (place/cancel/close/modify) gets its
        # OWN reserved executor that data/scanner calls can never consume. The
        # hard timeout above bounds stall duration; this split guarantees the
        # money path always has free workers regardless of data-side load.
        self._executor_trade = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="hl-trade"
        )
        self._executor_data = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="hl-data"
        )
        # Max adverse fill for market orders (fraction for SDK)
        self.market_slippage = max(0.0005, float(market_slippage_pct) / 100.0)
        self.base_url = (
            base_url
            or (constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL)
        ).rstrip("/")
        self.private_key = (private_key or "").strip()
        self.account_address = (account_address or "").strip()
        self._info = None
        self._exchange = None
        self._meta_cache: tuple[float, dict[str, Any]] | None = None
        # Short-TTL cache for meta_and_asset_ctxs (whole-universe fetch used for
        # funding). ticker() and funding_rate() both need it — without this each
        # /api/market call hit the upstream 2× on top of every poll.
        self._ctx_cache: tuple[float, Any] | None = None
        # Short-TTL cache for user_state, keyed by (ts, validated_state, addr) so a
        # changed account_address never serves another account's state. See
        # _user_state_cached() and the _USER_STATE_* constants above.
        self._user_state_cache: tuple[float, dict[str, Any], str] | None = None
        # One-shot flag so a sustained 429 burst logs the stale-serve warning once,
        # not on every degraded poll; reset on the next successful fetch.
        self._user_state_stale_warned: bool = False
        # OI history per coin: list of (unix_ts, open_interest), pruned to ~4.5h.
        # Feeds oi_change_pct_1h / _4h in market_extras(). In-memory only —
        # resets on restart (advisory context, not persisted state).
        self._oi_history: dict[str, list[tuple[float, float]]] = {}
        # Shared read-rate budget (proaktiv gegen 429). Read-path _to_thread calls
        # pass through this bucket; money-path calls bypass it. rps<=0 disables it.
        # Settings read defensively so client construction never fails on config.
        try:
            from app.config import get_settings

            _s = get_settings()
            _rps = float(getattr(_s, "hl_read_max_rps", 10.0))
            _burst = float(getattr(_s, "hl_read_burst", 20.0))
        except Exception:
            _rps, _burst = 10.0, 20.0
        self._read_limiter: _AsyncTokenBucket | None = (
            _AsyncTokenBucket(_rps, _burst) if _rps > 0 else None
        )

    def _meta_ctxs_sync(self, ttl: float = 2.0):
        """meta_and_asset_ctxs() with a short TTL cache (runs inside a thread)."""
        now = time.monotonic()
        if self._ctx_cache is not None and (now - self._ctx_cache[0]) < ttl:
            return self._ctx_cache[1]
        ctx = self._get_info().meta_and_asset_ctxs()
        self._ctx_cache = (now, ctx)
        return ctx

    def _record_oi_and_change(
        self, coin: str, oi: float | None, *, now: float | None = None
    ) -> tuple[float | None, float | None]:
        """Append the current OI to this coin's history and return the % change
        vs ~1h and ~4h ago. Returns (None, None) until enough history exists."""
        if oi is None or oi <= 0:
            return None, None
        now = time.monotonic() if now is None else float(now)
        hist = self._oi_history.setdefault(coin, [])
        # Sample-throttle (Plan-Review): max 1 sample/minute — the 5s market
        # poll would otherwise grow ~3200 tuples/coin in the 4.5h window. If
        # the newest sample is <60s old, skip the append and just compute.
        if not hist or (now - hist[-1][0]) >= 60.0:
            hist.append((now, float(oi)))
        cutoff = now - 4.5 * 3600.0
        while hist and hist[0][0] < cutoff:
            hist.pop(0)
        return (
            self._oi_change_over(hist, now, 3600.0),
            self._oi_change_over(hist, now, 4 * 3600.0),
        )

    @staticmethod
    def _oi_change_over(
        hist: list[tuple[float, float]], now: float, lookback_s: float
    ) -> float | None:
        """% change of the latest OI vs the sample ~lookback_s ago.

        Uses the last sample at/before the target time. If none is old enough,
        falls back to the oldest sample ONLY when it already spans >= 50% of the
        lookback (avoids a misleading '1h change' computed over 2 minutes)."""
        if not hist:
            return None
        target = now - lookback_s
        ref = None
        for ts, val in hist:
            if ts <= target:
                ref = (ts, val)
            else:
                break
        if ref is None:
            oldest_ts, oldest_val = hist[0]
            if now - oldest_ts < 0.5 * lookback_s:
                return None
            ref = (oldest_ts, oldest_val)
        ref_val = ref[1]
        cur = hist[-1][1]
        if ref_val <= 0:
            return None
        change = (cur - ref_val) / ref_val * 100.0
        return round(change, 3) if math.isfinite(change) else None

    def _apply_http_timeout(self, inst: Any) -> None:
        """Force a hard request timeout onto the SDK instance's HTTP session.

        The SDK's ``API.post`` calls ``self.session.post(..., timeout=self.timeout)``
        — with ``self.timeout`` defaulting to ``None`` (no timeout at all). We set
        ``inst.timeout`` so every call carries our bound, AND wrap the session's
        ``request`` so any path that omits/None's the timeout still gets one.

        If the SDK ever stops exposing a usable ``session`` (internals changed),
        we RAISE at construction rather than silently run without a timeout — a
        no-op here would re-open exactly the unbounded-stall hole Q-01 closes.
        """
        session = getattr(inst, "session", None)
        if session is None or not callable(getattr(session, "request", None)):
            raise HyperliquidError(
                "Hyperliquid SDK instance exposes no usable 'session.request' to "
                "apply an HTTP timeout to (SDK internals changed?); refusing to "
                "run without a hard upstream timeout"
            )
        inst.timeout = self._http_timeout_s
        if not getattr(session, "_mlt_timeout_wrapped", False):
            orig_request = session.request
            timeout_s = self._http_timeout_s

            @functools.wraps(orig_request)
            def _request(method, url, **kwargs):
                if kwargs.get("timeout") is None:
                    kwargs["timeout"] = timeout_s
                return orig_request(method, url, **kwargs)

            session.request = _request
            session._mlt_timeout_wrapped = True

    def _get_info(self):
        if self._info is None:
            from hyperliquid.info import Info

            # timeout= muss schon in den Konstruktor: Info.__init__ macht selbst
            # HTTP-POSTs (spot_meta/meta), bevor _apply_http_timeout greifen kann.
            info = Info(self.base_url, skip_ws=True, timeout=self._http_timeout_s)
            self._apply_http_timeout(info)
            self._info = info
        return self._info

    def _get_exchange(self):
        if self._exchange is None:
            if not self.private_key:
                raise HyperliquidError("HL_PRIVATE_KEY not configured")
            from eth_account import Account
            from hyperliquid.exchange import Exchange

            wallet = Account.from_key(self.private_key)
            addr = self.account_address or wallet.address
            self.account_address = addr
            exchange = Exchange(
                wallet, self.base_url, account_address=addr, timeout=self._http_timeout_s
            )
            self._apply_http_timeout(exchange)
            self._exchange = exchange
        return self._exchange

    def _resolve_address(self) -> str:
        addr = self.account_address
        if not addr and self.private_key:
            from eth_account import Account

            addr = Account.from_key(self.private_key).address
            self.account_address = addr
        return addr or ""

    async def aclose(self) -> None:
        self._info = None
        self._exchange = None
        # Q-01: shut down BOTH dedicated executors so their worker threads don't
        # outlive the client. cancel_futures drops still-queued work.
        self._executor_trade.shutdown(wait=False, cancel_futures=True)
        self._executor_data.shutdown(wait=False, cancel_futures=True)

    async def _to_thread(
        self, fn, *args, money_path: bool = False, paced: bool = False, **kwargs
    ):
        """Run SDK call on a dedicated, size-bounded executor.

        Q-01: money-path calls (place/cancel/close/modify) route to the RESERVED
        ``_executor_trade`` so a scanner/data fan-out that fills ``_executor_data``
        can never starve confirm/close. Using our own bounded executors (instead
        of ``asyncio.to_thread``'s shared default pool) also means one stalled HL
        endpoint can't exhaust the process-wide thread pool.

        ALL failures become HyperliquidError — the SDK raises its own
        ServerError/ClientError (e.g. testnet 502) which would otherwise escape
        the ExchangeError handlers as HTTP 500.
        """
        loop = asyncio.get_running_loop()
        executor = self._executor_trade if money_path else self._executor_data
        call = functools.partial(fn, *args, **kwargs) if (args or kwargs) else fn
        attempts = 0
        while True:
            # Pace ONLY reads that opt in (the scanner klines fan-out) through the
            # shared token bucket, and do it INSIDE the retry loop so retries also
            # re-pace — otherwise a 429 storm's retries would bypass the limiter and
            # multiply the upstream request rate exactly when it must not. Interactive
            # and monitor reads pass paced=False: they're low-volume and rely on the
            # 429-retry + stale cache, and must never queue behind a scan (priority
            # inversion). The money path is NEVER paced or retried — an order must
            # not wait behind a scan, nor be auto-resent (double-order risk).
            if paced and not money_path and self._read_limiter is not None:
                await self._read_limiter.acquire()
            try:
                return await loop.run_in_executor(executor, call)
            except HyperliquidError:
                raise
            except Exception as e:
                if (
                    not money_path
                    and attempts < _RATE_LIMIT_RETRIES
                    and _is_rate_limited(e)
                ):
                    await asyncio.sleep(_RATE_LIMIT_BACKOFF_S * (2**attempts))
                    attempts += 1
                    continue
                raise HyperliquidError(f"hyperliquid api: {e}") from e

    def _load_meta_sync(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._meta_cache is not None and (now - self._meta_cache[0]) < _META_TTL_S:
            return self._meta_cache[1]
        info = self._get_info()
        meta = info.meta()
        if not isinstance(meta, dict):
            raise HyperliquidError("unrecognized metadata response shape")
        raw_universe = meta.get("universe")
        if raw_universe is not None and not isinstance(raw_universe, (list, tuple)):
            raise HyperliquidError("unrecognized metadata response shape")
        self._meta_cache = (now, meta)
        return meta

    def _asset_row(self, coin: str) -> dict[str, Any]:
        meta = self._load_meta_sync()
        coin = to_hl_coin(coin)
        for row in meta.get("universe") or []:
            if not isinstance(row, dict):
                continue
            raw_name = row.get("name")
            if isinstance(raw_name, str) and raw_name.strip().upper() == coin:
                return row
        raise HyperliquidError(f"Unknown Hyperliquid coin: {coin}")

    async def ping(self) -> int:
        def _p():
            self._get_info().all_mids()
            return int(time.time() * 1000)

        return await self._to_thread(_p)

    async def list_symbols(self) -> list[str]:
        """All tradeable perp coins (for the UI symbol dropdown)."""

        def _s():
            meta = self._load_meta_sync()
            raw_universe = meta.get("universe")
            if raw_universe is None:
                universe = []
            elif isinstance(raw_universe, (list, tuple)):
                universe = raw_universe
            else:
                raise HyperliquidError("unrecognized symbol-list response shape")
            out = []
            for a in universe:
                if not isinstance(a, dict):
                    continue
                raw_name = a.get("name")
                if not isinstance(raw_name, str):
                    continue
                name = raw_name.strip().upper()
                if not name or a.get("isDelisted"):
                    continue
                out.append(name)
            return sorted(out)

        return await self._to_thread(_s)

    async def market_overview(self, limit: int = 12) -> list[dict[str, Any]]:
        """Top perps by 24h notional volume with funding/mark (for the scanner)."""

        def _o():
            info = self._get_info()
            meta_ctx = info.meta_and_asset_ctxs()
            if (
                not isinstance(meta_ctx, (list, tuple))
                or len(meta_ctx) < 2
                or not isinstance(meta_ctx[0], dict)
                or not isinstance(meta_ctx[1], (list, tuple))
            ):
                raise HyperliquidError("unrecognized market-overview response shape")
            raw_universe = meta_ctx[0].get("universe")
            if raw_universe is None:
                universe = []
            elif isinstance(raw_universe, (list, tuple)):
                universe = raw_universe
            else:
                raise HyperliquidError("unrecognized market-overview response shape")
            ctxs = meta_ctx[1]
            rows: list[dict[str, Any]] = []
            for i, u in enumerate(universe):
                if not isinstance(u, dict):
                    continue
                raw_name = u.get("name")
                if not isinstance(raw_name, str):
                    continue
                name = raw_name.strip().upper()
                if not name or u.get("isDelisted"):
                    continue
                raw_ctx = ctxs[i] if i < len(ctxs) else None
                ctx = raw_ctx if isinstance(raw_ctx, dict) else {}
                mark = _opt_f(ctx.get("markPx"))
                if mark is not None and mark <= 0:
                    mark = None
                # 24h price-change % from prevDayPx (Task 24 universe momentum
                # ranking). prevDayPx is the only price-change horizon HL exposes
                # in the batch ctx — 1h/4h would need per-coin history we don't
                # fetch here, so the universe uses the 24h move as the runner
                # proxy. None when prevDayPx is missing/zero (graceful).
                prev = _opt_f(ctx.get("prevDayPx"))
                volume24 = _opt_f(ctx.get("dayNtlVlm"))
                open_interest = _opt_f(ctx.get("openInterest"))
                pchg = (
                    (mark - prev) / prev * 100.0
                    if prev and mark and prev > 0
                    else None
                )
                if pchg is not None and not math.isfinite(pchg):
                    pchg = None
                rows.append(
                    {
                        "symbol": name,
                        "volume24": (
                            volume24 if volume24 is not None and volume24 >= 0 else 0.0
                        ),
                        "funding": _opt_f(ctx.get("funding")) or 0.0,
                        "last": mark,
                        # Task 24: momentum + positioning fields for the universe.
                        # open_interest is a LEVEL (OI-Δ needs history -> not here);
                        # it alone does not trigger the scanner oi_read (which
                        # also requires oi_change_pct_1h), so classic stays intact.
                        "price_change_pct": pchg,
                        "open_interest": (
                            open_interest
                            if open_interest is not None and open_interest >= 0
                            else None
                        ),
                    }
                )
            rows.sort(key=lambda r: r["volume24"], reverse=True)
            return rows[: max(1, int(limit))]

        return await self._to_thread(_o)

    async def contract_meta(self, symbol: str) -> ContractMeta:
        def _m():
            row = self._asset_row(symbol)
            coin = row["name"].strip().upper()
            sz_dec = _required_int(
                row.get("szDecimals"), "szDecimals", minimum=0
            )
            vol_unit = 10 ** (-sz_dec) if sz_dec > 0 else 1.0
            max_lev = _required_int(
                row.get("maxLeverage"), "maxLeverage", minimum=1
            )
            is_delisted = row.get("isDelisted")
            if is_delisted is not None and not isinstance(is_delisted, bool):
                raise HyperliquidError("isDelisted is invalid")
            return ContractMeta(
                symbol=coin,
                contract_size=1.0,  # size is in coins
                # I3: HL has no fixed tick size (5-sig-fig rounding instead), so
                # price_unit=0.0 is a deliberate boundary, not a TODO. The
                # gate's tick-size guard is a no-op for this field; drift is
                # covered by AUTO_FLATTEN, and order placement rounds via
                # round_hl_price() rather than this unit.
                price_unit=0.0,  # filled from mid later if needed
                vol_unit=vol_unit,
                min_vol=vol_unit,
                max_vol=1_000_000.0,
                max_leverage=max_lev,
                min_leverage=1,
                min_notional=HL_MIN_NOTIONAL_USD,
                api_allowed=is_delisted is not True,
                price_scale=None,
                vol_scale=sz_dec,
                base_coin=coin,
                quote_coin="USDC",
                state=0,
            )

        return await self._to_thread(_m)

    async def ticker(self, symbol: str) -> Ticker:
        def _t():
            coin = to_hl_coin(symbol)
            info = self._get_info()
            mids = info.all_mids()
            if coin not in mids:
                raise HyperliquidError(f"No mid price for {coin}")
            mid = _required_finite_float(mids[coin], f"mid price for {coin}")
            if mid <= 0:
                raise HyperliquidError(f"Invalid mid price for {coin}")
            # funding from metaAndAssetCtxs if available
            funding = None
            try:
                meta_ctx = self._meta_ctxs_sync()
                universe = meta_ctx[0].get("universe") if meta_ctx else []
                ctxs = meta_ctx[1] if meta_ctx and len(meta_ctx) > 1 else []
                for i, u in enumerate(universe or []):
                    if not isinstance(u, dict):
                        continue
                    raw_name = u.get("name")
                    if (
                        isinstance(raw_name, str)
                        and raw_name.strip().upper() == coin
                        and i < len(ctxs)
                    ):
                        funding = _opt_f(ctxs[i].get("funding")) or 0.0
                        break
            except Exception:
                pass
            return Ticker(
                symbol=coin,
                last_price=mid,
                bid1=mid,
                ask1=mid,
                fair_price=mid,
                index_price=mid,
                funding_rate=funding,
                timestamp=int(time.time() * 1000),
            )

        return await self._to_thread(_t)

    async def funding_rate(self, symbol: str) -> FundingRate:
        """Funding read straight from the shared meta_and_asset_ctxs cache.

        O5: previously this routed through ticker(), which calls all_mids() (a
        full-universe price fetch) purely for a mid price funding doesn't need —
        so build_market_snapshot triggered all_mids() twice per HL snapshot. The
        funding value already lives in the SAME 2s ctx cache market_extras() and
        ticker() use, so read it there directly and skip the extra round-trip.
        Falls back to 0.0 if the ctx doesn't carry funding for the coin.
        """

        def _f():
            coin = to_hl_coin(symbol)
            funding = 0.0
            try:
                meta_ctx = self._meta_ctxs_sync()
                universe = meta_ctx[0].get("universe") if meta_ctx else []
                ctxs = meta_ctx[1] if meta_ctx and len(meta_ctx) > 1 else []
                for i, u in enumerate(universe or []):
                    if not isinstance(u, dict):
                        continue
                    raw_name = u.get("name")
                    if (
                        isinstance(raw_name, str)
                        and raw_name.strip().upper() == coin
                        and i < len(ctxs)
                    ):
                        funding = _opt_f(ctxs[i].get("funding")) or 0.0
                        break
            except Exception:
                funding = 0.0
            return FundingRate(
                symbol=coin,
                funding_rate=funding,
                timestamp=int(time.time() * 1000),
            )

        return await self._to_thread(_f)

    async def market_extras(self, symbol: str) -> dict[str, Any]:
        """Open interest / premium context from meta_and_asset_ctxs.

        Reads the SAME short-TTL ctx cache as ticker()/funding_rate() so this
        adds no extra upstream hit. oi_change_pct_* come from an in-memory
        per-coin OI history (_record_oi_and_change) and are None until enough
        history has accumulated. MEXC has no equivalent and does NOT implement
        this method (see app/analysis/context._fetch_market_extras).
        """

        def _x():
            coin = to_hl_coin(symbol)
            meta_ctx = self._meta_ctxs_sync()
            universe = meta_ctx[0].get("universe") if meta_ctx else []
            ctxs = meta_ctx[1] if meta_ctx and len(meta_ctx) > 1 else []
            oi = premium = prev_day_px = None
            for i, u in enumerate(universe or []):
                if not isinstance(u, dict):
                    continue
                raw_name = u.get("name")
                if (
                    isinstance(raw_name, str)
                    and raw_name.strip().upper() == coin
                    and i < len(ctxs)
                ):
                    c = ctxs[i] or {}
                    oi = _opt_f(c.get("openInterest"))
                    premium = _opt_f(c.get("premium"))
                    prev_day_px = _opt_f(c.get("prevDayPx"))
                    break
            ch1, ch4 = self._record_oi_and_change(coin, oi)
            return {
                "open_interest": oi,
                "premium": premium,
                "prev_day_px": prev_day_px,
                "oi_change_pct_1h": ch1,
                "oi_change_pct_4h": ch4,
            }

        return await self._to_thread(_x)

    async def klines(
        self, symbol: str, interval: str, limit_hint: int = 200, *, paced: bool = False
    ) -> list[Candle]:
        def _k():
            coin = to_hl_coin(symbol)
            iv = INTERVAL_MAP.get(interval, interval)
            # map UI intervals to HL
            hl_iv = {
                "Min5": "5m",
                "Min15": "15m",
                "Min60": "1h",
                "Hour4": "4h",
                "Day1": "1d",
                "5m": "5m",
                "15m": "15m",
                "1H": "1h",
                "4H": "4h",
                "1D": "1d",
                "1h": "1h",
                "4h": "4h",
                "1d": "1d",
            }.get(iv, "15m")
            end = int(time.time() * 1000)
            # bar ms
            bar_ms = {
                "5m": 5 * 60_000,
                "15m": 15 * 60_000,
                "1h": 60 * 60_000,
                "4h": 4 * 60 * 60_000,
                "1d": 24 * 60 * 60_000,
            }.get(hl_iv, 15 * 60_000)
            start = end - bar_ms * max(int(limit_hint), 10)
            info = self._get_info()
            raw = info.candles_snapshot(coin, hl_iv, start, end)
            if not isinstance(raw, list):
                raise HyperliquidError("kline response has an unrecognized shape")
            out: list[Candle] = []
            for r in raw or []:
                if not isinstance(r, dict):
                    raise HyperliquidError("kline response contains a non-object row")
                timestamp = _required_finite_float(r.get("t"), "kline time")
                open_px = _required_finite_float(r.get("o"), "kline open")
                high_px = _required_finite_float(r.get("h"), "kline high")
                low_px = _required_finite_float(r.get("l"), "kline low")
                close_px = _required_finite_float(r.get("c"), "kline close")
                volume = _required_finite_float(r.get("v"), "kline vol")
                if timestamp <= 0 or min(open_px, high_px, low_px, close_px) <= 0:
                    raise HyperliquidError("kline time and prices must be > 0")
                if high_px < max(open_px, close_px) or low_px > min(
                    open_px, close_px
                ):
                    raise HyperliquidError("kline OHLC geometry is invalid")
                if volume < 0:
                    raise HyperliquidError("kline volume must be >= 0")
                out.append(
                    Candle(
                        time=int(timestamp),
                        open=open_px,
                        high=high_px,
                        low=low_px,
                        close=close_px,
                        vol=volume,
                        amount=0.0,
                    )
                )
            out.sort(key=lambda candle: candle.time)
            if any(a.time == b.time for a, b in zip(out, out[1:])):
                raise HyperliquidError("duplicate kline timestamp")
            if limit_hint > 0 and len(out) > limit_hint:
                out = out[-limit_hint:]
            return out

        return await self._to_thread(_k, paced=paced)

    @staticmethod
    def _validate_user_state(state: Any) -> dict[str, Any]:
        """M-2 fail-safe: a 200-OK-but-degraded/partial ``user_state`` body
        (backend hiccup, truncated JSON) must NOT be trusted as a flat account —
        that would report zero equity / no positions and hide a real position
        from the risk & auto-flatten logic. A complete ``user_state`` always
        carries BOTH a margin summary AND an ``assetPositions`` list; if either
        is absent (or the body is not even a dict), refuse it and raise — mirror
        MEXC's stop-order lookup which never trusts an unrecognized shape. This
        surfaces as UNKNOWN/degraded downstream, never as a confident "flat".
        """
        margin = (
            state.get("marginSummary") or state.get("crossMarginSummary")
            if isinstance(state, dict)
            else None
        )
        if (
            not isinstance(state, dict)
            or not isinstance(margin, dict)
            or "accountValue" not in margin
            or not isinstance(state.get("assetPositions"), list)
        ):
            raise HyperliquidError(
                "user_state returned an unrecognized/degraded shape "
                f"({type(state).__name__}); refusing to treat as a flat account"
            )
        _required_finite_float(margin.get("accountValue"), "account value")
        return state

    def _user_state_cached(
        self,
        info: Any,
        addr: str,
        ttl: float = _USER_STATE_TTL_S,
        *,
        fresh: bool = False,
    ) -> dict[str, Any]:
        """info.user_state(addr) behind a short TTL cache with 429/error ride-out.

        Runs INSIDE the _to_thread worker (sync). Non-fresh + cache younger than
        ttl → return the cached, _validate_user_state-validated state. Otherwise
        fetch + validate + cache. On a fetch/validate EXCEPTION (esp. the 429
        ClientError) fall back to a bounded-stale cached state (age <=
        _USER_STATE_MAX_STALE_S) so a transient rate-limit burst degrades to a
        slightly stale read for the display/monitor paths instead of 502-ing; if
        no usable cache exists it re-raises, so a genuine "no data" still fails
        honestly.

        fresh=True (money-DECISION reads: entry sizing/gate equity, aggregate
        exposure, live SL-modify size) forces a REAL live fetch and disables the
        stale ride-out. Two properties matter for money-safety, and BOTH are
        required:
          1) it BYPASSES the 2s TTL short-circuit — a <=2s-old cache filled by an
             EARLIER confirm under the same _trade_lock reflects the PRE-trade
             account (positions/equity before that confirm's order), so serving
             it here would let a second confirm size against risk=0 and double the
             real exposure. fresh must therefore always hit the exchange.
          2) it fails CLOSED on a 429 rather than sizing an order against
             up-to-8s-stale, likely optimistic-high equity.

        NOTE: this is deliberately NOT used by the F-08 close-path TOCTOU re-read,
        which must stay a fresh live read to catch an externally flipped position.
        """
        allow_stale = not fresh
        cache = self._user_state_cache
        if (
            not fresh
            and cache is not None
            and cache[2] == addr
            and (time.monotonic() - cache[0]) < ttl
        ):
            return self._validate_user_state(cache[1])
        try:
            state = self._validate_user_state(info.user_state(addr))
        except Exception as e:
            # Measure staleness AFTER the (possibly slow / up-to-http_timeout_s
            # blocking) fetch, so the 8s bound reflects the ACTUAL age of what
            # we'd serve — a timed-out fetch must not serve ~18s-old money data
            # while logging it as "aged 7.9s".
            now = time.monotonic()
            if (
                allow_stale
                and cache is not None
                and cache[2] == addr
                and (now - cache[0]) <= _USER_STATE_MAX_STALE_S
            ):
                if not self._user_state_stale_warned:
                    log.warning(
                        "user_state fetch failed (%s); serving cached state "
                        "aged %.1fs (<= %.1fs bound) to ride out the burst",
                        e,
                        now - cache[0],
                        _USER_STATE_MAX_STALE_S,
                    )
                    self._user_state_stale_warned = True
                return self._validate_user_state(cache[1])
            raise
        self._user_state_cache = (time.monotonic(), state, addr)
        self._user_state_stale_warned = False
        return state

    def _invalidate_user_state_cache(self) -> None:
        """Drop the cached user_state after a position-changing mutation.

        Defense-in-depth for the fresh-read TTL fix: money-DECISION reads already
        force a live fetch, but this makes even NON-fresh readers (monitor/display,
        and any follow-up read within the 2s TTL) observe the POST-trade account
        promptly instead of a place/close/stop/cancel-stale snapshot. Kept cheap:
        a single atomic attribute clear, the next read simply refetches once.
        """
        self._user_state_cache = None

    def _resolve_addr(self) -> str | None:
        """Account address, deriving + caching it from the private key if needed.

        Shared by assets()/positions()/account_state() so all three resolve the
        SAME address before hitting _user_state_cached.
        """
        addr = self.account_address
        if not addr and self.private_key:
            from eth_account import Account

            addr = Account.from_key(self.private_key).address
            self.account_address = addr
        return addr

    def _fetch_state_sync(self, *, fresh: bool) -> dict[str, Any] | None:
        """One address-resolved _user_state_cached fetch (runs in the worker
        thread). Returns None when no account is configured (→ caller yields an
        empty result, exactly as before)."""
        if not self.account_address and not self.private_key:
            return None
        info = self._get_info()
        addr = self._resolve_addr()
        return self._user_state_cached(info, addr, fresh=fresh)

    @staticmethod
    def _assets_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
        margin = state.get("marginSummary") or state.get("crossMarginSummary") or {}
        raw_values = (
            margin.get("accountValue"),
            state.get("withdrawable"),
            margin.get("totalMarginUsed"),
            margin.get("totalNtlPos"),
        )
        if any(isinstance(value, bool) for value in raw_values):
            raise ValueError("boolean financial value in user state")
        equity = float(raw_values[0] or 0)
        withdrawable = float(raw_values[1] or 0)
        used = float(raw_values[2] or 0)
        notional = float(raw_values[3] or 0)
        if not all(math.isfinite(v) for v in (equity, withdrawable, used, notional)):
            raise ValueError("non-finite financial value in user state")
        # The exchange-reported value is authoritative. Zero/missing must stay
        # zero instead of being turned into spendable margin via equity - IM.
        available = max(withdrawable, 0.0)
        return [
            {
                "currency": "USDT",  # normalized label; HL margin ccy is USDC
                "equity": equity,
                "availableBalance": available,
                "cashBalance": withdrawable,
                # H-2: totalNtlPos is total NOTIONAL position value, NOT
                # unrealized PnL (that is per-position `unrealizedPnl`, read
                # in positions()). Name it honestly so no future PnL/UI
                # caller mistakes notional exposure for realized/unreal PnL.
                "notional_position": notional,
            }
        ]

    @staticmethod
    def _positions_from_state(
        state: dict[str, Any], symbol: str | None = None
    ) -> list[dict[str, Any]]:
        rows = []
        coin_f = to_hl_coin(symbol) if symbol else None
        for ap in state.get("assetPositions") or []:
            pos = ap.get("position") or {}
            coin_raw = pos.get("coin")
            coin = coin_raw.strip().upper() if isinstance(coin_raw, str) else ""
            # Only a valid, explicit other coin is proven foreign. Missing or
            # malformed identity must remain in the fail-closed validation path.
            if coin_f and coin and coin != coin_f:
                continue
            szi = _required_finite_float(pos.get("szi"), "position size")
            if abs(szi) < 1e-12:
                continue
            if not coin:
                raise HyperliquidError("Invalid open position symbol")
            entry = _required_finite_float(pos.get("entryPx"), "position entry")
            if entry <= 0:
                raise HyperliquidError("Invalid position entry")
            leverage = pos.get("leverage")
            leverage_type = leverage.get("type") if isinstance(leverage, dict) else None
            if leverage_type not in ("isolated", "cross"):
                raise HyperliquidError("Invalid open position leverage type")
            leverage_value = _opt_f(leverage.get("value"))
            if leverage_value is not None and leverage_value <= 0:
                leverage_value = None
            liquidation_price = _opt_f(pos.get("liquidationPx"))
            if liquidation_price is not None and liquidation_price <= 0:
                liquidation_price = None
            initial_margin = _opt_f(pos.get("marginUsed"))
            if initial_margin is not None and initial_margin <= 0:
                initial_margin = None
            side = "long" if szi > 0 else "short"
            rows.append(
                {
                    "positionId": coin,
                    "symbol": coin,
                    "positionType": 1 if side == "long" else 2,
                    "holdVol": abs(szi),
                    "holdAvgPrice": entry,
                    "openAvgPrice": entry,
                    "leverage": leverage_value,
                    "openType": 2 if leverage_type == "cross" else 1,
                    "unRealizedPnl": _opt_f(pos.get("unrealizedPnl")),
                    "liquidatePrice": liquidation_price,
                    "im": initial_margin,
                }
            )
        return rows

    async def assets(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        def _a():
            state = self._fetch_state_sync(fresh=fresh)
            return [] if state is None else self._assets_from_state(state)

        try:
            return await self._to_thread(_a)
        except Exception as e:
            raise HyperliquidError(f"user_state failed: {e}") from e

    async def positions(
        self, symbol: str | None = None, *, fresh: bool = False
    ) -> list[dict[str, Any]]:
        def _p():
            state = self._fetch_state_sync(fresh=fresh)
            return [] if state is None else self._positions_from_state(state, symbol)

        try:
            return await self._to_thread(_p)
        except Exception as e:
            raise HyperliquidError(f"positions failed: {e}") from e

    async def account_state(
        self, symbol: str | None = None, *, fresh: bool = False
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """ONE clearinghouseState fetch → (assets, positions).

        Finding 2 (latency): assets() and positions() each call
        _user_state_cached, so a Preview/Confirm that needs both issued TWO
        identical clearinghouseState fetches. Both are derived from the SAME
        blob, so this combines them into a single fetch and hands back both.

        Money-safety is unchanged: fresh=True still forces a real live fetch
        with the same fail-closed 429 behaviour (no stale serve) as the
        separate reads. TOCTOU can only improve — assets and positions now come
        from ONE snapshot, so equity and open exposure are mutually consistent
        instead of read ~a roundtrip apart. assets()/positions() are untouched
        for every other caller.
        """

        def _as():
            state = self._fetch_state_sync(fresh=fresh)
            if state is None:
                return [], []
            return (
                self._assets_from_state(state),
                self._positions_from_state(state, symbol),
            )

        try:
            return await self._to_thread(_as)
        except Exception as e:
            raise HyperliquidError(f"user_state failed: {e}") from e

    async def user_fills(
        self, symbol: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Recent executions of the account (userFills), newest first.

        Read-only /info call — also shows fills placed outside this app.
        Normalized shape for the UI: symbol, px, sz, side, time(ms), dir,
        closed_pnl, oid, fee.
        """

        def _f():
            if not self.account_address and not self.private_key:
                return []
            addr = self.account_address
            if not addr and self.private_key:
                from eth_account import Account

                addr = Account.from_key(self.private_key).address
                self.account_address = addr
            info = self._get_info()
            fills = info.user_fills(addr)
            if not isinstance(fills, list):
                raise HyperliquidError(
                    "user_fills returned an unrecognized shape "
                    f"({type(fills).__name__})"
                )
            coin_f = to_hl_coin(symbol) if symbol else None
            out: list[dict[str, Any]] = []
            for f in fills:
                if not isinstance(f, dict):
                    continue
                coin_raw = f.get("coin")
                coin = coin_raw.strip().upper() if isinstance(coin_raw, str) else ""
                if coin_f and coin != coin_f:
                    continue
                side_raw = str(f.get("side") or "")
                px = _opt_f(f.get("px"))
                sz = _opt_f(f.get("sz"))
                if isinstance(f.get("time"), bool):
                    continue
                try:
                    fill_time = int(f.get("time"))
                except (TypeError, ValueError, OverflowError):
                    continue
                if (
                    not coin
                    or side_raw not in ("A", "B")
                    or px is None
                    or px <= 0
                    or sz is None
                    or sz <= 0
                    or fill_time <= 0
                ):
                    continue
                fee_raw = f.get("fee")
                fee = 0.0 if fee_raw in (None, "") else _opt_f(fee_raw)
                try:
                    out.append(
                        {
                            "symbol": coin,
                            "px": px,
                            "sz": sz,
                            "side": "buy" if side_raw == "B" else "sell",
                            "time": fill_time,
                            "dir": str(f.get("dir") or ""),
                            # F2: signed position size BEFORE this fill. ==0 marks
                            # a Flat->Open (trade-epoch) fill; used to derive a
                            # STABLE reopen signature. Kept as a float when present.
                            "start_position": _opt_f(f.get("startPosition")),
                            "closed_pnl": _opt_f(f.get("closedPnl")),
                            "oid": None
                            if isinstance(f.get("oid"), bool)
                            else f.get("oid"),
                            "fee": fee,
                        }
                    )
                except (TypeError, ValueError, OverflowError):
                    continue
            out.sort(key=lambda r: r["time"], reverse=True)
            return out[: max(1, int(limit))]

        try:
            return await self._to_thread(_f)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"user_fills failed: {e}") from e

    async def account_snapshot(self, *, fresh: bool = False) -> dict[str, Any]:
        from app.mexc.client import map_account_snapshot

        assets, positions = await self.account_state(fresh=fresh)
        return map_account_snapshot(assets, positions)

    async def set_leverage(
        self,
        symbol: str,
        leverage: int,
        open_type: int,
        position_type: int | None = None,
        position_id: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise HyperliquidError("symbol is required for leverage change")
        if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage <= 0:
            raise HyperliquidError("leverage must be a positive integer")
        if (
            isinstance(open_type, bool)
            or not isinstance(open_type, int)
            or open_type not in (1, 2)
        ):
            raise HyperliquidError("open_type must be 1 (isolated) or 2 (cross)")

        def _l():
            ex = self._get_exchange()
            coin = to_hl_coin(symbol)
            is_cross = open_type == 2
            return ex.update_leverage(leverage, coin, is_cross)

        try:
            result = await self._to_thread(_l, money_path=True)
        except Exception as e:
            raise HyperliquidError(f"set_leverage failed: {e}") from e
        # Official updateLeverage success is an explicit
        # {status: "ok", response: {type: "default"}}. Anything else is either
        # a rejection or an uncertain mutation result and must block the entry.
        error = _status_error(result)
        response = result.get("response") if isinstance(result, dict) else None
        if error:
            raise HyperliquidError(f"set_leverage rejected: {error}", raw=result)
        if (
            not isinstance(result, dict)
            or result.get("status") != "ok"
            or not isinstance(response, dict)
            or response.get("type") != "default"
        ):
            raise HyperliquidError("uncertain set_leverage response shape", raw=result)
        return result

    async def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """Accept internal/MEXC-shaped body and map to HL SDK order/market_open."""

        def _place():
            ex = self._get_exchange()
            raw_symbol = body.get("symbol")
            if not isinstance(raw_symbol, str) or not raw_symbol.strip():
                raise HyperliquidError("order symbol must be a non-empty string")
            coin = to_hl_coin(raw_symbol)
            side_raw = body.get("side")
            # MEXC open sides only: 1 long open, 3 short open; also long/short.
            # Reject close sides (2/4) and unknown values — bool(2) would wrongly buy.
            if not isinstance(side_raw, bool) and side_raw in (
                1,
                "1",
                "long",
                "LONG",
            ):
                is_buy = True
            elif not isinstance(side_raw, bool) and side_raw in (
                3,
                "3",
                "short",
                "SHORT",
            ):
                is_buy = False
            else:
                raise HyperliquidError(
                    f"unsupported order side {side_raw!r} "
                    "(use 1/long or 3/short; close via close_position_market)"
                )
            sz = _required_finite_float(
                body.get("vol") or body.get("sz") or 0, "order size"
            )
            if sz <= 0:
                raise HyperliquidError("size/vol must be > 0")
            otype = body.get("type")
            if not isinstance(otype, bool) and otype in (5, "5", "market", "Market"):
                is_market = True
            elif not isinstance(otype, bool) and otype in (1, "1", "limit", "Limit"):
                is_market = False
            else:
                raise HyperliquidError(f"unsupported order type {otype!r}")
            reduce_only = bool(body.get("reduceOnly") or body.get("r") or False)
            price_raw = body.get("price")
            px = (
                0.0
                if price_raw in (None, "")
                else _required_finite_float(price_raw, "order price")
            )

            # Tick rounding per HL rules (5 sig figs / max decimals via szDecimals)
            row = self._asset_row(coin)
            sz_dec = _required_int(
                row.get("szDecimals"), "szDecimals", minimum=0
            )
            if px > 0:
                px = round_hl_price(px, sz_dec)

            sl = body.get("stopLossPrice")
            tp = body.get("takeProfitPrice")
            sl_value = (
                None
                if sl in (None, "")
                else _required_finite_float(sl, "stop-loss price")
            )
            tp_value = (
                None
                if tp in (None, "")
                else _required_finite_float(tp, "take-profit price")
            )
            if sl_value is not None and sl_value < 0:
                raise HyperliquidError("stop-loss price must be >= 0")
            # X2-05: side-aware rounding TOWARD entry so the exchange-tick
            # precision cut can never make the real trigger riskier (SL) or
            # overstate reward (TP) beyond the already risk-approved SL/TP.
            sl_px = (
                round_hl_price_side_aware(sl_value, sz_dec, is_buy=is_buy, kind="sl")
                if sl_value is not None and sl_value > 0
                else None
            )
            tp_px = (
                round_hl_price_side_aware(tp_value, sz_dec, is_buy=is_buy, kind="tp")
                if tp_value is not None and tp_value > 0
                else None
            )
            tp2 = body.get("takeProfitPrice2")
            tp2_value = (
                None
                if tp2 in (None, "")
                else _required_finite_float(tp2, "second take-profit price")
            )
            tp2_px = (
                round_hl_price_side_aware(
                    tp2_value, sz_dec, is_buy=is_buy, kind="tp"
                )
                if tp2_value is not None and tp2_value > 0
                else None
            )
            share_raw = body.get("tp1Share")
            share = (
                0.0
                if share_raw in (None, "")
                else _required_finite_float(share_raw, "first take-profit share")
            )

            # Stamp OUR externalOid onto the exchange order as a Cloid so a
            # transport timeout can recover the real (possibly filled) order and
            # never bait a re-confirm into a double position.
            entry_cloid = external_oid_to_cloid(str(body.get("externalOid") or ""))

            if is_market:
                # Cap adverse fill: SDK crosses at mid*(1±slippage)
                result = ex.market_open(
                    coin,
                    is_buy,
                    sz,
                    px if px > 0 else None,
                    self.market_slippage,
                    cloid=entry_cloid,
                )
            else:
                if px <= 0:
                    raise HyperliquidError("limit order requires price > 0")
                result = ex.order(
                    coin,
                    is_buy,
                    sz,
                    px,
                    {"limit": {"tif": "Gtc"}},
                    reduce_only=reduce_only,
                    cloid=entry_cloid,
                )
            entry_err = _order_response_error(result)
            if entry_err:
                prefix = (
                    "uncertain order-create response"
                    if entry_err == "unrecognized order response"
                    else "order rejected"
                )
                raise HyperliquidError(f"{prefix}: {entry_err}", raw=result)

            # ── F-02: size protective triggers to the ACTUAL entry fill ──────
            # Entry and SL/TP are separate orders. A reduce-only stop sized to
            # the REQUESTED size is unsafe when the entry did not fully fill:
            #  - a resting/zero-fill limit → the stop has no position (orphan),
            #    or, if a same-side position already exists, it attaches to the
            #    OLD position and could close it at the new stop;
            #  - a partial fill → the stop over-reduces vs what actually opened.
            # So we size every trigger to the fill reported by THIS entry order.
            filled_sz = _extract_filled_sz(result)
            if is_market and filled_sz <= 0:
                raise HyperliquidError(
                    "uncertain order-create response: market fill missing",
                    raw=result,
                )
            protect_sz = filled_sz

            # Attach TP/SL as reduce-only trigger orders.
            # Each result is checked for a real oid — no blind echo.
            trigger_errors: list[str] = []
            sl_trigger_oid = None
            tp_trigger_oid = None

            def _place_trigger(trigger_px: float, tpsl: str, trig_sz: float):
                return ex.order(
                    coin,
                    not is_buy,  # close direction
                    trig_sz,
                    trigger_px,
                    {
                        "trigger": {
                            "isMarket": True,
                            "triggerPx": trigger_px,
                            "tpsl": tpsl,
                        }
                    },
                    reduce_only=True,
                )

            # No fill (resting limit entry) → NO triggers. There is no position
            # to protect; the service surfaces this as unprotected/pending and
            # cancels the resting entry via its unfilled-entry handling.
            if protect_sz <= 0:
                return {
                    "orderId": _extract_oid(result),
                    "response": result,
                    "slTriggerOid": None,
                    "tpTriggerOid": None,
                    "tpTriggerOid2": None,
                    "triggerErrors": [],
                    "requestedStopLoss": sl_px,
                    "requestedTakeProfit": tp_px,
                    "requestedTakeProfit2": None,
                    "entryFilledSz": 0.0,
                    "unfilled": True,
                    "symbol": coin,
                    "exchange": "hyperliquid",
                }

            if sl_px is not None:
                try:
                    sl_res = _place_trigger(sl_px, "sl", protect_sz)
                    sl_trigger_oid = _extract_oid(sl_res)
                    err = _order_response_error(sl_res)
                    if sl_trigger_oid is None or err:
                        trigger_errors.append(f"sl: {err or 'no oid in response'}")
                        sl_trigger_oid = None
                except Exception as te:
                    trigger_errors.append(f"sl: {te}")

            # Optional TP ladder (scale-out): split the reduce-only TP across two
            # rungs (tp1 at tp_px, tp2 at tp2_px) instead of one. Falls back to a
            # single TP if the split would round a rung to zero size.
            vol_unit = 10 ** (-sz_dec) if sz_dec > 0 else 1.0
            do_ladder = tp_px is not None and tp2_px is not None and 0.0 < share < 1.0
            tp_sz1 = tp_sz2 = None
            if do_ladder:
                from app.risk.sizing import round_down_to_unit

                tp_sz1 = round_down_to_unit(protect_sz * share, vol_unit)
                tp_sz2 = round_down_to_unit(protect_sz - tp_sz1, vol_unit)
                if tp_sz1 <= 0 or tp_sz2 <= 0:
                    do_ladder = False  # too small to split -> single TP fallback

            tp_trigger_oid2 = None
            if tp_px is not None:
                try:
                    tp_res = _place_trigger(tp_px, "tp", tp_sz1 if do_ladder else protect_sz)
                    tp_trigger_oid = _extract_oid(tp_res)
                    err = _order_response_error(tp_res)
                    if tp_trigger_oid is None or err:
                        trigger_errors.append(f"tp: {err or 'no oid in response'}")
                        tp_trigger_oid = None
                except Exception as te:
                    trigger_errors.append(f"tp: {te}")
            if do_ladder and tp2_px is not None:
                try:
                    tp2_res = _place_trigger(tp2_px, "tp", tp_sz2)
                    tp_trigger_oid2 = _extract_oid(tp2_res)
                    err = _order_response_error(tp2_res)
                    if tp_trigger_oid2 is None or err:
                        trigger_errors.append(f"tp2: {err or 'no oid in response'}")
                        tp_trigger_oid2 = None
                except Exception as te:
                    trigger_errors.append(f"tp2: {te}")

            return {
                "orderId": _extract_oid(result),
                "response": result,
                "slTriggerOid": sl_trigger_oid,
                "tpTriggerOid": tp_trigger_oid,
                "tpTriggerOid2": tp_trigger_oid2,
                "triggerErrors": trigger_errors,
                "requestedStopLoss": sl_px,
                "requestedTakeProfit": tp_px,
                "requestedTakeProfit2": tp2_px if do_ladder else None,
                "entryFilledSz": filled_sz,
                "symbol": coin,
                "exchange": "hyperliquid",
            }

        try:
            result = await self._to_thread(_place, money_path=True)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"place_order failed: {e}", raw=str(e)) from e
        # Post-trade: the account now holds a new/changed position — evict the
        # stale snapshot so non-fresh readers don't serve the pre-trade state.
        self._invalidate_user_state_cache()
        return result

    async def place_stop_order(
        self,
        symbol: str,
        *,
        position_side: str,
        vol: float,
        trigger_px: float,
        tpsl: str = "sl",
        reduce_only: bool = True,
    ) -> dict[str, Any]:
        """Place ONE reduce-only trigger (SL/TP) for an ALREADY-OPEN position.

        position_side is the side of the open position; the trigger CLOSES it,
        so its direction is the opposite. Used by OrderService.modify_stop_loss
        to add a fresh protective stop BEFORE the old one is cancelled — the
        position is therefore never unprotected during a SL move.
        """

        def _place():
            trigger_kind = str(tpsl).strip().lower()
            if trigger_kind not in ("sl", "tp"):
                raise HyperliquidError("tpsl must be 'sl' or 'tp'")
            if reduce_only is not True:
                raise HyperliquidError("protective trigger must be reduce-only")
            ex = self._get_exchange()
            coin = to_hl_coin(symbol)
            pos = (position_side or "").lower()
            if pos not in ("long", "short"):
                raise HyperliquidError(
                    f"position_side must be long/short, got {position_side!r}"
                )
            sz = _required_finite_float(vol or 0, "stop volume")
            if sz <= 0:
                raise HyperliquidError("vol must be > 0")
            # long closes by selling (is_buy False); short closes by buying.
            is_buy_close = pos == "short"
            row = self._asset_row(coin)
            sz_dec = _required_int(
                row.get("szDecimals"), "szDecimals", minimum=0
            )
            # X2-05: side-aware toward entry — position_side (not the close
            # order's is_buy_close) decides direction, since the trigger must
            # stay conservative relative to the OPEN position, not the closing
            # leg. tpsl chooses SL vs TP geometry.
            trigger_value = _required_finite_float(trigger_px, "trigger price")
            trg = round_hl_price_side_aware(
                trigger_value, sz_dec, is_buy=(pos == "long"), kind=trigger_kind
            )
            if trg <= 0:
                raise HyperliquidError("trigger_px must be > 0")
            result = ex.order(
                coin,
                is_buy_close,
                sz,
                trg,
                {
                    "trigger": {
                        "isMarket": True,
                        "triggerPx": trg,
                        "tpsl": trigger_kind,
                    }
                },
                reduce_only=True,
            )
            err = _order_response_error(result)
            oid = _extract_oid(result)
            if err or oid is None:
                return {
                    "orderId": None,
                    "response": result,
                    "error": err or "no oid in response",
                    "requestedTrigger": trg,
                    "symbol": coin,
                }
            return {
                "orderId": oid,
                "response": result,
                "error": None,
                "requestedTrigger": trg,
                "symbol": coin,
            }

        try:
            result = await self._to_thread(_place, money_path=True)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"place_stop_order failed: {e}", raw=str(e)) from e
        self._invalidate_user_state_cache()
        return result

    async def cancel_order(
        self, body: dict[str, Any] | list[Any]
    ) -> dict[str, Any] | list[Any]:
        def _c():
            ex = self._get_exchange()

            def cancel_target(item: Any) -> tuple[str, int]:
                if not isinstance(item, dict):
                    raise HyperliquidError(
                        "HL cancel needs {orderId, symbol} per order"
                    )
                raw_oid = item.get("orderId")
                if raw_oid is None:
                    raw_oid = item.get("oid")
                raw_symbol = item.get("symbol")
                if not isinstance(raw_symbol, str) or not raw_symbol.strip():
                    raise HyperliquidError(
                        "HL cancel requires a positive orderId and symbol"
                    )
                coin = to_hl_coin(raw_symbol)
                if not _is_positive_oid(raw_oid) or not coin:
                    raise HyperliquidError(
                        "HL cancel requires a positive orderId and symbol"
                    )
                return coin, int(raw_oid)

            def cancel_one(coin: str, oid: int) -> dict[str, Any]:
                result = ex.cancel(coin, oid)
                error = _status_error(result)
                if error:
                    raise HyperliquidError(f"cancel rejected: {error}", raw=result)
                response = result.get("response") if isinstance(result, dict) else None
                data = response.get("data") if isinstance(response, dict) else None
                statuses = data.get("statuses") if isinstance(data, dict) else None
                if (
                    not isinstance(result, dict)
                    or result.get("status") != "ok"
                    or not isinstance(response, dict)
                    or response.get("type") != "cancel"
                    or statuses != ["success"]
                ):
                    raise HyperliquidError(
                        "uncertain cancel response shape", raw=result
                    )
                return result

            # list of oids or dict with orderId/symbol
            if isinstance(body, list):
                if not body:
                    raise HyperliquidError("HL cancel request must not be empty")
                results = []
                for item in body:
                    coin, oid = cancel_target(item)
                    results.append(cancel_one(coin, oid))
                return results
            coin, oid = cancel_target(body)
            return cancel_one(coin, oid)

        try:
            result = await self._to_thread(_c, money_path=True)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"cancel failed: {e}") from e
        self._invalidate_user_state_cache()
        return result

    async def open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        def _o():
            info = self._get_info()
            addr = self.account_address
            if not addr and self.private_key:
                from eth_account import Account

                addr = Account.from_key(self.private_key).address
            if not addr:
                return []
            orders = info.open_orders(addr)
            if not isinstance(orders, list):
                raise HyperliquidError(
                    "open_orders returned an unrecognized shape "
                    f"({type(orders).__name__})"
                )
            coin_f = to_hl_coin(symbol) if symbol else None
            out = []
            for o in orders:
                if not isinstance(o, dict):
                    raise HyperliquidError("open_orders contains a non-object row")
                coin_raw = o.get("coin")
                coin = coin_raw.strip().upper() if isinstance(coin_raw, str) else ""
                if not coin:
                    raise HyperliquidError("open_orders contains an invalid row")
                if coin_f and coin != coin_f:
                    continue
                oid = o.get("oid")
                side = str(o.get("side") or "")
                vol = _opt_f(o.get("sz"))
                price = _opt_f(o.get("limitPx"))
                reduce_only = o.get("reduceOnly")
                if (
                    not _is_positive_oid(oid)
                    or side not in ("A", "B")
                    or vol is None
                    or vol <= 0
                    or price is None
                    or price <= 0
                    or not isinstance(reduce_only, bool)
                ):
                    raise HyperliquidError("open_orders contains an invalid row")
                out.append(
                    {
                        "orderId": oid,
                        "symbol": coin,
                        # HL side is A=ask/sell, B=bid/buy
                        "side": side,
                        "reduceOnly": reduce_only,
                        "vol": vol,
                        "price": price,
                        "raw": o,
                    }
                )
            return out

        try:
            return await self._to_thread(_o)
        except Exception as e:
            raise HyperliquidError(f"open_orders failed: {e}") from e

    async def open_stop_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Open trigger (SL/TP) orders via frontend_open_orders (includes triggers)."""

        def _s():
            info = self._get_info()
            addr = self.account_address
            if not addr and self.private_key:
                from eth_account import Account

                addr = Account.from_key(self.private_key).address
            if not addr:
                return []
            # C-1 fail-safe: the trigger-aware endpoint is the ONLY authoritative
            # source for SL/TP triggers. It must NOT be silently swapped for the
            # non-trigger-aware open_orders() on failure — that endpoint does not
            # carry isTrigger/triggerPx rows, so the heuristic below would find no
            # triggers and return a CONFIDENT empty list, indistinguishable from a
            # genuine "no stop orders". A protected position would then read as
            # MISSING and auto_flatten_if_sl_unverified could market-close it on a
            # transient endpoint hiccup. Mirror MEXC's gold-standard behaviour: if
            # the trigger-aware lookup cannot be completed with a RECOGNIZED
            # response, let it raise (the outer handler turns it into a
            # HyperliquidError → the caller resolves SL status to UNKNOWN, never
            # MISSING, and UNKNOWN never auto-flattens).
            orders = info.frontend_open_orders(addr)
            if not isinstance(orders, list):
                # 200-OK-but-degraded/unrecognized body (e.g. {} or None) is NOT
                # proof of "no triggers" — refuse it rather than trust an empty
                # confident list.
                raise HyperliquidError(
                    "frontend_open_orders returned an unrecognized shape "
                    f"({type(orders).__name__}); refusing to treat as 'no stops'"
                )
            coin_f = to_hl_coin(symbol) if symbol else None
            out = []
            for o in orders:
                if not isinstance(o, dict):
                    raise HyperliquidError(
                        "frontend_open_orders contains a non-object row"
                    )
                coin_raw = o.get("coin")
                coin = coin_raw.strip().upper() if isinstance(coin_raw, str) else ""
                if coin_f and coin and coin != coin_f:
                    continue
                otype = str(o.get("orderType") or "")
                trigger_flag = o.get("isTrigger")
                if trigger_flag is not None and not isinstance(trigger_flag, bool):
                    raise HyperliquidError("invalid isTrigger in frontend_open_orders")
                label_is_trigger = (
                    "stop" in otype.lower() or "take" in otype.lower()
                )
                if trigger_flag is False and label_is_trigger:
                    raise HyperliquidError(
                        "contradictory trigger fields in frontend_open_orders"
                    )
                is_trigger = trigger_flag is True or label_is_trigger
                if not is_trigger:
                    continue
                reduce_only = o.get("reduceOnly")
                if not isinstance(reduce_only, bool):
                    raise HyperliquidError(
                        "invalid reduceOnly in frontend_open_orders"
                    )
                if not reduce_only:
                    continue
                if not coin:
                    raise HyperliquidError(
                        "invalid protective trigger in frontend_open_orders"
                    )
                if not _is_positive_oid(o.get("oid")):
                    raise HyperliquidError(
                        "invalid protective trigger in frontend_open_orders"
                    )
                trigger_price = _opt_f(o.get("triggerPx"))
                if trigger_price is None or trigger_price <= 0:
                    raise HyperliquidError(
                        "invalid protective trigger in frontend_open_orders"
                    )
                # SL-coverage: expose the trigger's closing size as `vol` (the
                # field the frontend Deckungsgrad check reads first) so a HL stop
                # that only covers PART of an enlarged position can be detected.
                # For a RESTING reduce-only trigger, `sz` is the (remaining) size
                # in COINS that the stop will close — the same unit as hold_vol
                # (abs(szi)). `origSz` is the ORIGINAL size (filled = origSz - sz,
                # cf. _hl_order_state_is_dead), so `sz` — not origSz/notional — is
                # the coverage to compare against hold_vol. Missing/empty/garbage
                # → None (never 0): a false 0 would read as "0 covered" → false
                # partial-coverage alarm; None lets the frontend fall through to
                # its conservative "protected" fallback.
                vol = _opt_f(o.get("sz"))
                # Make the "never 0" contract true AT THE SOURCE (defense-in-depth):
                # a resting trigger with sz<=0 (e.g. a whole-position TP/SL whose
                # coin size HL reports as 0) must NOT surface as vol=0 — that would
                # read as "0 covered" for any consumer that sums vol without a >0
                # gate. Emit None so the coverage check falls through to
                # "protected" instead of raising a false partial-coverage alarm.
                if vol is not None and vol <= 0:
                    vol = None
                out.append(
                    {
                        "orderId": o.get("oid"),
                        "symbol": coin,
                        "triggerPrice": trigger_price,
                        "orderType": otype,
                        "reduceOnly": True,
                        "vol": vol,
                        "raw": o,
                    }
                )
            return out

        try:
            return await self._to_thread(_s)
        except Exception as e:
            raise HyperliquidError(f"open_stop_orders failed: {e}") from e

    async def close_position_market(
        self,
        symbol: str,
        *,
        side: str,
        vol: float,
        open_type: int = 1,
        external_oid: str | None = None,
    ) -> dict[str, Any]:
        # O-08: optional deterministic cloid (mirrors the entry path's
        # externalOid -> Cloid stamping) so a timeout during a close can be
        # recovered/looked-up unambiguously via order_by_external_oid instead
        # of guessing whether the close actually went through.
        # "close:"-Namespace: selbst wenn ein Aufrufer die Entry-externalOid
        # durchreicht, kollidiert der Close-Cloid nie mit dem Entry-Cloid
        # (gleiche OID => gleicher Hash => ambiger Lookup waere die Folge).
        close_cloid = (
            external_oid_to_cloid(f"close:{external_oid}") if external_oid else None
        )

        def _cl():
            ex = self._get_exchange()
            coin = to_hl_coin(symbol)
            want = (side or "").lower()
            if want not in ("long", "short"):
                raise HyperliquidError(
                    f"close side must be long/short, got {side!r}"
                )
            # ── F-08 TOCTOU guard: the service checked the side earlier, but the
            # SDK's market_close ignores `side` and closes WHATEVER position is
            # live. Re-read the live side/size immediately before closing; if the
            # position flipped externally between check and execution, refuse
            # instead of market-closing the new opposite side.
            info = self._get_info()
            addr = self._resolve_address()
            if not addr:
                raise HyperliquidError("account address unavailable for live close check")
            state = self._validate_user_state(info.user_state(addr))
            live_szi = 0.0
            for ap in state["assetPositions"]:
                pos = ap.get("position") or {}
                raw_coin = pos.get("coin")
                if (
                    isinstance(raw_coin, str)
                    and raw_coin.strip().upper() == coin
                ):
                    live_szi = _required_finite_float(
                        pos.get("szi"), "live position size"
                    )
                    break
            live_side = (
                "long" if live_szi > 0 else ("short" if live_szi < 0 else None)
            )
            if live_side is None:
                raise HyperliquidError(
                    f"no open {want} position on {coin} to close"
                )
            if live_side != want:
                raise HyperliquidError(
                    f"refusing close: requested {want} but live position is "
                    f"{live_side} (side flipped externally) — not closing the "
                    "wrong side"
                )
            live_sz = abs(live_szi)
            if vol is None:
                close_sz = live_sz
            else:
                requested_close = _required_finite_float(vol, "close volume")
                if requested_close <= 0:
                    raise HyperliquidError("close volume must be > 0")
                close_sz = min(requested_close, live_sz)
            if close_sz <= 0:
                raise HyperliquidError("close size resolved to 0")
            # Emergency close: allow wider slippage so the flatten actually fills.
            slippage = max(0.01, self.market_slippage)
            # market_close is a reduce-only IOC close of the (now-verified) side.
            result = ex.market_close(
                coin, sz=close_sz, slippage=slippage, cloid=close_cloid
            )
            # ── F-03: HL returns order rejections INSIDE an outwardly-ok
            # response. Treat an inner error as a FAILED close so the service
            # never logs `closed` / answers ok while the position is still open.
            err = _order_response_error(result)
            if err:
                prefix = (
                    "uncertain close response"
                    if err == "unrecognized order response"
                    else "close rejected"
                )
                raise HyperliquidError(f"{prefix}: {err}", raw=result)
            return result

        try:
            result = await self._to_thread(_cl, money_path=True)
        except HyperliquidError:
            raise
        except Exception as e:
            raise HyperliquidError(f"close failed: {e}") from e
        self._invalidate_user_state_cache()
        return result

    async def order_by_external_oid(self, symbol: str, external_oid: str) -> Any:
        """Find OUR order (open or filled) by externalOid, via its Cloid.

        Primary: orderStatus-by-cloid returns resting AND filled orders, so a
        market order that already filled during a transport timeout is found —
        this is what prevents a re-confirm double position. Fallback: scan open
        + trigger orders for the cloid. Returns {} when nothing matches
        (fail-closed: recovery must not false-positive).
        """
        oid = (external_oid or "").strip()
        if not oid:
            return {}
        cloid = external_oid_to_cloid(oid)
        cloid_raw = cloid.to_raw() if cloid is not None else None

        if cloid is not None:
            def _q():
                addr = self._resolve_address()
                if not addr:
                    return None
                return self._get_info().query_order_by_cloid(addr, cloid)

            try:
                status = await self._to_thread(_q, money_path=True)
            except HyperliquidError:
                status = None
            if isinstance(status, dict) and str(status.get("status")).lower() == "order":
                order_wrapper = status.get("order")
                if not _hl_recovery_identity_matches(
                    order_wrapper, symbol, cloid_raw
                ):
                    status = None
                elif _hl_order_state_is_dead(order_wrapper):
                    # X-05: the deterministic cloid lookup resolved to a
                    # terminal-dead order (canceled/rejected/…). Mirror MEXC's
                    # _mexc_state_is_dead: return {} so the caller runs its
                    # fail-closed hard error and the trader can consciously
                    # re-place, instead of reporting a phantom "recovered" fill
                    # for an order that is actually dead. A dead cloid result is
                    # definitive — do not fall through to the open-orders scan.
                    return {}
                else:
                    return {
                        "externalOid": oid,
                        "cloid": cloid_raw,
                        "match": "cloid",
                        "order": order_wrapper,
                        "raw": status,
                    }

        orders = await self.open_orders(symbol)
        try:
            stops = await self.open_stop_orders(symbol)
        except Exception:
            stops = []
        wanted = {oid}
        if cloid_raw:
            wanted.add(cloid_raw)
        hits: list[dict[str, Any]] = []
        for o in list(orders or []) + list(stops or []):
            if not isinstance(o, dict):
                continue
            raw = o.get("raw") if isinstance(o.get("raw"), dict) else {}
            row_symbol = o.get("symbol") or raw.get("coin")
            if (
                not isinstance(row_symbol, str)
                or not row_symbol.strip()
                or to_hl_coin(row_symbol) != to_hl_coin(symbol)
            ):
                continue
            matched = False
            for src in (o, raw):
                for key in (
                    "cloid",
                    "clientOrderId",
                    "client_order_id",
                    "externalOid",
                    "external_oid",
                ):
                    val = src.get(key)
                    if val is not None and str(val) in wanted:
                        hits.append({**o, "externalOid": oid})
                        matched = True
                        break
                if matched:
                    break
        return hits if hits else {}


# Hyperliquid orderStatus inner-status vocabulary (X-05). The deterministic
# query_order_by_cloid resolves an order to a single lifecycle status. LIVE /
# recoverable states — open, filled, triggered, resting — are valid recovery
# hits. TERMINAL-DEAD states are every canceled/rejected variant HL emits
# (canceled, marginCanceled, reduceOnlyCanceled, scheduledCancel, rejected,
# tickRejected, perpMarginRejected, …). Rather than enumerate a list HL keeps
# extending, key off the unambiguous "cancel"/"reject" tokens: NO live state
# contains either, and every dead one contains exactly one. This is the HL
# analogue of _mexc_state_is_dead. Fail-safe: a missing/unexpected status is NOT
# dead — never discard a genuinely recoverable order (that reopens the silent
# phantom-loss the whole guard exists to prevent).
_HL_DEAD_STATE_TOKENS = ("cancel", "reject")


def _hl_order_state_is_dead(order_wrapper: Any) -> bool:
    """True only for a ZERO-FILL canceled/rejected HL order.

    `order_wrapper` is ``status["order"]`` from query_order_by_cloid, shaped
    ``{"order": {<order fields>}, "status": <lifecycle>}``. Unknown/missing or
    non-string status → False (fail-safe: not treated as dead).

    A cancel/reject STATUS alone is not terminal-dead: an IOC/market order can
    partially fill and then cancel the remainder (canceled/marginCanceled with a
    REAL open position). Only a genuine zero-fill (``filled == origSz - sz == 0``)
    is dead → {} → fail-closed hard error. A partial fill (``filled > 0``) must
    run the normal recovery/verify+protect path, else the filled part is left
    unprotected and can bait a double entry. Fail-safe: origSz/sz missing or
    unparseable → NOT dead (never discard a possibly-filled order).
    """
    if not isinstance(order_wrapper, dict):
        return False
    st = order_wrapper.get("status")
    if not isinstance(st, str):
        return False
    st_l = st.strip().lower()
    if not any(tok in st_l for tok in _HL_DEAD_STATE_TOKENS):
        return False
    order = order_wrapper.get("order")
    if not isinstance(order, dict):
        return False  # fail-safe: cannot prove zero-fill → not dead
    orig = _opt_f(order.get("origSz"))
    rem = _opt_f(order.get("sz"))
    if orig is None or rem is None:
        return False  # fail-safe: sizes missing/unparseable → not dead
    # Zero-fill (within float noise) is the only terminal-dead case.
    if orig <= 0 or rem < 0 or rem > orig:
        return False  # malformed sizes cannot prove a zero fill
    return (orig - rem) <= 1e-12


def _hl_recovery_identity_matches(
    order_wrapper: Any, symbol: str, cloid_raw: str | None
) -> bool:
    """Require a concrete same-symbol order and reject an explicit cloid conflict."""
    if not isinstance(order_wrapper, dict):
        return False
    order = order_wrapper.get("order")
    if not isinstance(order, dict):
        return False
    returned_coin = order.get("coin")
    if (
        not isinstance(returned_coin, str)
        or not returned_coin.strip()
        or to_hl_coin(returned_coin) != to_hl_coin(symbol)
    ):
        return False
    if not _is_positive_oid(order.get("oid")):
        return False
    returned_cloid = order.get("cloid")
    return (
        returned_cloid is None
        or cloid_raw is not None
        and str(returned_cloid).lower() == cloid_raw.lower()
    )


def _status_error(result: Any) -> str | None:
    """Return error text from an HL order response, or None if accepted."""
    if not isinstance(result, dict):
        return None
    if result.get("status") not in (None, "ok"):
        return str(result.get("status"))
    try:
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        for st in statuses:
            if isinstance(st, dict) and "error" in st:
                return str(st["error"])
    except Exception:
        return None
    return None


def _is_positive_oid(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    if isinstance(value, str):
        return value.isdigit() and int(value) > 0
    return False


def _order_response_error(result: Any) -> str | None:
    """Validate one official Hyperliquid order result, including success shape."""
    error = _status_error(result)
    if error:
        return error
    if not isinstance(result, dict) or result.get("status") != "ok":
        return "unrecognized order response"
    response = result.get("response")
    data = response.get("data") if isinstance(response, dict) else None
    statuses = data.get("statuses") if isinstance(data, dict) else None
    if (
        not isinstance(response, dict)
        or response.get("type") != "order"
        or not isinstance(statuses, list)
        or len(statuses) != 1
        or not isinstance(statuses[0], dict)
    ):
        return "unrecognized order response"
    status = statuses[0]
    if isinstance(status.get("filled"), dict):
        filled = status["filled"]
        total_sz = _opt_f(filled.get("totalSz"))
        if not _is_positive_oid(filled.get("oid")) or total_sz is None or total_sz <= 0:
            return "unrecognized order response"
        return None
    if isinstance(status.get("resting"), dict):
        return (
            None
            if _is_positive_oid(status["resting"].get("oid"))
            else "unrecognized order response"
        )
    return "unrecognized order response"


def _extract_filled_sz(result: Any) -> float:
    """Actually-filled size (coins) of an HL entry order from its response.

    Sums ``totalSz`` across all ``filled`` statuses. A resting (unfilled) limit
    order carries only a ``resting`` status → returns 0.0. Used to size the
    reduce-only protective triggers to the REAL fill (F-02) so a partial/zero
    fill never places an oversized or orphan stop that could act on a different
    (pre-existing) position.
    """
    if not isinstance(result, dict):
        return 0.0
    try:
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    except AttributeError:
        return 0.0
    total = 0.0
    for st in statuses or []:
        if isinstance(st, dict) and isinstance(st.get("filled"), dict):
            try:
                total += float(st["filled"].get("totalSz") or 0)
            except (TypeError, ValueError):
                continue
    return max(0.0, total)


def _extract_oid(result: Any) -> Any:
    if not isinstance(result, dict):
        return None
    # common SDK shape: {"status":"ok","response":{"data":{"statuses":[{"resting":{"oid":...}}]}}}
    try:
        statuses = (
            result.get("response", {})
            .get("data", {})
            .get("statuses", [])
        )
        if statuses:
            st0 = statuses[0]
            if "resting" in st0:
                oid = st0["resting"].get("oid")
                return oid if _is_positive_oid(oid) else None
            if "filled" in st0:
                oid = st0["filled"].get("oid")
                return oid if _is_positive_oid(oid) else None
            if "error" in st0:
                return None
    except Exception:
        pass
    oid = result.get("orderId") or result.get("oid")
    return oid if _is_positive_oid(oid) else None

"""Q-01: hard upstream timeout + size-bounded, SEPARATED executors.

A hanging Hyperliquid endpoint must not block indefinitely, and a scanner
fan-out of HL calls must never starve the money path (confirm/close). These
tests pin:
  1. every SDK call carries a finite HTTP timeout (== hl_http_timeout_s),
  2. construction FAILS LOUDLY if the SDK session attribute is gone (no silent
     no-op — the timeout would otherwise never be applied),
  3. HL calls run on dedicated, bounded executors (not the global default pool),
  4. mutation and fresh-account paths use RESERVED executors a scanner cannot fill.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.hyperliquid.client import HyperliquidClient
from app.hyperliquid.errors import HyperliquidError


class _RecordingSession:
    """Stand-in for the SDK's requests.Session that records request kwargs."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.close_calls = 0

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return {"ok": True}

    def close(self) -> None:
        self.close_calls += 1


class _FakeInfo:
    """Mimics hyperliquid.api.API: exposes .session and .timeout, no network."""

    def __init__(self, base_url, skip_ws=True, timeout=None, **_):
        self.base_url = base_url
        self.session = _RecordingSession()
        self.timeout = timeout


class _NoSessionInfo:
    """A degraded SDK whose session attribute is missing (internals changed)."""

    def __init__(self, base_url, skip_ws=True, timeout=None, **_):
        self.base_url = base_url


class _ObservedThreadLock:
    """Expose the second lock attempt without timing-dependent sleeps."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self._attempts = 0
        self.second_waiter = threading.Event()

    def __enter__(self):
        with self._guard:
            self._attempts += 1
            if self._attempts == 2:
                self.second_waiter.set()
        self._lock.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self._lock.release()


def _patch_info(monkeypatch, cls) -> None:
    import hyperliquid.info as hl_info

    monkeypatch.setattr(hl_info, "Info", cls)


def test_hl_session_has_request_timeout(monkeypatch):
    _patch_info(monkeypatch, _FakeInfo)
    c = HyperliquidClient(private_key="0x" + "1" * 64, testnet=True, http_timeout_s=7.5)
    info = c._get_info()
    # API.post() passes timeout=self.timeout — it must carry our hard timeout.
    assert info.timeout == 7.5
    # Belt-and-suspenders: the session enforces a finite timeout even if a call
    # omits it or passes None explicitly (as API.post does when timeout is None).
    info.session.request("POST", "http://x/info", timeout=None)
    info.session.request("POST", "http://x/info")
    for _method, _url, kwargs in info.session.calls:
        assert kwargs.get("timeout") == 7.5


def test_hl_missing_session_raises_loudly(monkeypatch):
    """If the SDK stops exposing a session, construction must RAISE — never run
    silently without an upstream timeout (Auflage: no silent no-op)."""
    _patch_info(monkeypatch, _NoSessionInfo)
    c = HyperliquidClient(private_key="0x" + "1" * 64, testnet=True)
    with pytest.raises(HyperliquidError):
        c._get_info()


@pytest.mark.asyncio
async def test_concurrent_first_reads_share_one_sdk_info_instance(monkeypatch):
    init_lock = _ObservedThreadLock()
    constructor_calls = 0

    def construct_info(*args, **kwargs):
        nonlocal constructor_calls
        constructor_calls += 1
        assert init_lock.second_waiter.wait(timeout=2.0)
        return _FakeInfo(*args, **kwargs)

    _patch_info(monkeypatch, construct_info)
    client = HyperliquidClient(testnet=True)
    client._info_init_lock = init_lock
    try:
        first, second = await asyncio.gather(
            asyncio.to_thread(client._get_info),
            asyncio.to_thread(client._get_info),
        )
    finally:
        await client.aclose()

    assert first is second
    assert constructor_calls == 1


@pytest.mark.asyncio
async def test_hl_uses_bounded_dedicated_executors():
    """HL calls run on dedicated bounded executors, not the global default pool;
    data and money-path calls land on DISTINCT executors (distinct thread names)."""
    c = HyperliquidClient(testnet=True)
    try:
        assert isinstance(c._executor_data, ThreadPoolExecutor)
        assert isinstance(c._executor_trade, ThreadPoolExecutor)
        assert isinstance(c._executor_account, ThreadPoolExecutor)
        assert c._executor_data is not c._executor_trade
        assert c._executor_account not in (c._executor_data, c._executor_trade)

        data_name = await c._to_thread(lambda: threading.current_thread().name)
        trade_name = await c._to_thread(
            lambda: threading.current_thread().name, money_path=True
        )
        account_name = await c._to_thread(
            lambda: threading.current_thread().name, account_read=True
        )
        assert data_name.startswith("hl-data")
        assert trade_name.startswith("hl-trade")
        assert account_name.startswith("hl-account")
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_hl_close_releases_all_sdk_http_sessions():
    """Closing the adapter must release Info, Exchange and Exchange.info pools."""
    c = HyperliquidClient(testnet=True)
    info_session = _RecordingSession()
    exchange_session = _RecordingSession()
    nested_info_session = _RecordingSession()
    c._info = SimpleNamespace(session=info_session)
    c._exchange = SimpleNamespace(
        session=exchange_session,
        info=SimpleNamespace(session=nested_info_session),
    )

    await c.aclose()

    assert info_session.close_calls == 1
    assert exchange_session.close_calls == 1
    assert nested_info_session.close_calls == 1


@pytest.mark.asyncio
async def test_money_path_not_starved_by_data_saturation():
    """AUFLAGE: a scanner/data fan-out that fills the data executor must NOT
    block the money path. Saturate _executor_data with blocking tasks; a
    money-path call must still complete promptly on its reserved executor."""
    c = HyperliquidClient(testnet=True)
    ev = threading.Event()
    try:
        blockers = [
            asyncio.create_task(c._to_thread(lambda: ev.wait(10)))
            for _ in range(32)
        ]
        await asyncio.sleep(0.15)  # let them occupy every data worker
        result = await asyncio.wait_for(
            c._to_thread(lambda: "trade-ok", money_path=True), timeout=2.0
        )
        assert result == "trade-ok"
        ev.set()
        await asyncio.gather(*blockers)
    finally:
        ev.set()
        await c.aclose()


@pytest.mark.asyncio
async def test_fresh_account_read_not_starved_by_data_saturation():
    """Confirm's mandatory fresh risk snapshot must not queue behind scanner I/O."""
    c = HyperliquidClient(testnet=True)
    release = threading.Event()
    all_started = threading.Event()
    fresh_started = threading.Event()
    guard = threading.Lock()
    started = 0

    def block_data_worker():
        nonlocal started
        with guard:
            started += 1
            if started == 8:
                all_started.set()
        return release.wait(timeout=2.0)

    class _Info:
        def user_state(self, _addr):
            fresh_started.set()
            return {
                "marginSummary": {
                    "accountValue": "1000",
                    "totalMarginUsed": "0",
                    "totalNtlPos": "0",
                },
                "withdrawable": "1000",
                "assetPositions": [],
            }

    blockers = []
    try:
        blockers = [
            asyncio.create_task(c._to_thread(block_data_worker)) for _ in range(8)
        ]
        assert await asyncio.to_thread(all_started.wait, 2.0)
        c.account_address = "0x" + "2" * 40
        c._info = _Info()
        fresh_read = asyncio.create_task(c.assets(fresh=True))
        bypassed = await asyncio.to_thread(fresh_started.wait, 0.25)
        release.set()
        rows = await fresh_read
        await asyncio.gather(*blockers)

        assert bypassed
        assert rows[0]["equity"] == 1000.0
    finally:
        release.set()
        if blockers:
            await asyncio.gather(*blockers, return_exceptions=True)
        await c.aclose()

"""Q-01: hard upstream timeout + size-bounded, SEPARATED executors.

A hanging Hyperliquid endpoint must not block indefinitely, and a scanner
fan-out of HL calls must never starve the money path (confirm/close). These
tests pin:
  1. every SDK call carries a finite HTTP timeout (== hl_http_timeout_s),
  2. construction FAILS LOUDLY if the SDK session attribute is gone (no silent
     no-op — the timeout would otherwise never be applied),
  3. HL calls run on dedicated, bounded executors (not the global default pool),
  4. the money path uses a RESERVED executor a data/scanner fan-out cannot fill.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.hyperliquid.client import HyperliquidClient
from app.hyperliquid.errors import HyperliquidError


class _RecordingSession:
    """Stand-in for the SDK's requests.Session that records request kwargs."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return {"ok": True}


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
async def test_hl_uses_bounded_dedicated_executors():
    """HL calls run on dedicated bounded executors, not the global default pool;
    data and money-path calls land on DISTINCT executors (distinct thread names)."""
    c = HyperliquidClient(testnet=True)
    try:
        assert isinstance(c._executor_data, ThreadPoolExecutor)
        assert isinstance(c._executor_trade, ThreadPoolExecutor)
        assert c._executor_data is not c._executor_trade

        data_name = await c._to_thread(lambda: threading.current_thread().name)
        trade_name = await c._to_thread(
            lambda: threading.current_thread().name, money_path=True
        )
        assert data_name.startswith("hl-data")
        assert trade_name.startswith("hl-trade")
    finally:
        await c.aclose()


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

"""Build the active exchange client from settings."""

from __future__ import annotations

from typing import Any

from app.config import Settings


def create_exchange_client(settings: Settings) -> Any:
    ex = (settings.exchange or "mexc").strip().lower()
    if ex in ("hyperliquid", "hl"):
        from app.hyperliquid.client import HyperliquidClient

        return HyperliquidClient(
            private_key=settings.hl_private_key,
            account_address=settings.hl_account_address,
            testnet=settings.hl_testnet,
            base_url=settings.hl_base_url or None,
            market_slippage_pct=settings.market_entry_slippage_pct,
            http_timeout_s=settings.hl_http_timeout_s,
        )
    from app.mexc.client import MexcClient

    return MexcClient(
        base_url=settings.mexc_base_url,
        api_key=settings.mexc_api_key,
        api_secret=settings.mexc_api_secret,
    )


def exchange_ready(settings: Settings) -> bool:
    ex = (settings.exchange or "mexc").strip().lower()
    if ex in ("hyperliquid", "hl"):
        return bool(settings.hl_private_key)
    return settings.mexc_ready

"""Hyperliquid client errors."""

from __future__ import annotations

from typing import Any


class HyperliquidError(Exception):
    def __init__(self, message: str, *, raw: Any = None):
        super().__init__(message)
        self.raw = raw

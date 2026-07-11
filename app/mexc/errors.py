from typing import Any


class MexcError(Exception):
    """Raised when MEXC API returns an error or keys are missing."""

    def __init__(self, message: str, raw: Any = None):
        super().__init__(message)
        self.raw = raw

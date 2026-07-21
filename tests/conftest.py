import os
import sys
import tempfile
from pathlib import Path

# Ensure project root is on sys.path for `import app`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Test isolation (opt-out, not opt-in): every test run gets its own throwaway
# SQLite file so the analyze/journal write-paths — and the audit proposals/
# orders tables — can NEVER pollute the developer's real data/trader.db. This
# is the root fix for the COIN###_USDT pollution bug: test_analyze_cache POSTs
# hundreds of fake symbols through /api/analyze, and without this they landed
# in the real DB and fed the background journal resolver spam. A test that
# needs a specific DB still overrides DATABASE_PATH explicitly.
_TEST_DB = os.path.join(tempfile.gettempdir(), "obsidian_trader_test.db")
try:
    if os.path.exists(_TEST_DB):
        os.remove(_TEST_DB)
except OSError:
    pass
os.environ.setdefault("DATABASE_PATH", _TEST_DB)

# Tests must not require X-Local-Token from a developer .env
os.environ["LOCAL_API_TOKEN"] = ""

# The FastAPI TestClient sends Host: testserver, which the prod TrustedHost
# allowlist (loopback-only) rejects by design. Opt it in HERE — centrally, once
# — instead of trusting "testserver" in the prod default or rewriting per-test
# base URLs. Set before app import so the module-level middleware picks it up.
os.environ.setdefault("TRUSTED_HOSTS_EXTRA", "testserver")

# Prefer deterministic exchange defaults in unit tests unless overridden
os.environ.setdefault("EXCHANGE", "mexc")
os.environ.setdefault("HL_TESTNET", "true")
os.environ.setdefault("TRADING_ENABLED", "false")

try:
    from app.config import get_settings

    get_settings.cache_clear()
except Exception:
    pass

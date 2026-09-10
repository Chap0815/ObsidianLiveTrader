import os
import sys
import tempfile
from pathlib import Path

# Ensure project root is on sys.path for `import app`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Test isolation (opt-out, not opt-in): every pytest process gets its own
# throwaway data directory, including SQLite sidecars and instance.lock. Force
# the path instead of honoring a developer DATABASE_PATH from the parent shell;
# tests must never write to real runtime data. A test that needs a specific DB
# still overrides DATABASE_PATH explicitly via monkeypatch.
_TEST_DIR = tempfile.TemporaryDirectory(prefix="obsidian-trader-test-")
_TEST_DB = os.path.join(_TEST_DIR.name, "trader.db")
os.environ["DATABASE_PATH"] = _TEST_DB

# Tests must not require X-Local-Token from a developer .env
os.environ["LOCAL_API_TOKEN"] = ""

# The FastAPI TestClient sends Host: testserver, which the prod TrustedHost
# allowlist (loopback-only) rejects by design. Opt it in HERE — centrally, once
# — instead of trusting "testserver" in the prod default or rewriting per-test
# base URLs. Set before app import so the module-level middleware picks it up.
os.environ.setdefault("TRUSTED_HOSTS_EXTRA", "testserver")

# Never inherit a developer's live mode, privacy opt-in or credentials. Tests
# that need configured providers/exchanges set synthetic values via monkeypatch.
os.environ["EXCHANGE"] = "mexc"
os.environ["HL_TESTNET"] = "true"
os.environ["TRADING_ENABLED"] = "false"
os.environ["MAINNET_ACK"] = "false"
os.environ["INCLUDE_ACCOUNT_IN_LLM"] = "false"
os.environ["LLM_PROVIDER"] = "claude"
for _secret_var in (
    "MEXC_API_KEY",
    "MEXC_API_SECRET",
    "HL_PRIVATE_KEY",
    "HL_ACCOUNT_ADDRESS",
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "XAI_API_KEY",
    "OPENAI_API_KEY",
):
    os.environ[_secret_var] = ""
# TestClient starts the production lifespan before individual tests can inject
# their exchange doubles. Keep both background readers off unless a lifecycle
# test explicitly enables and mocks one of them.
os.environ["JOURNAL_ENABLED"] = "false"
os.environ["TM_ENABLED"] = "false"

try:
    from app.config import Settings, get_settings

    # BaseSettings otherwise opens ROOT/.env even when individual values are
    # overridden above. Tests opt into only their own synthetic tmp env files.
    Settings.model_config["env_file"] = None
    get_settings.cache_clear()
except Exception:
    pass

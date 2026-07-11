import os
import sys
from pathlib import Path

# Ensure project root is on sys.path for `import app`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Tests must not require X-Local-Token from a developer .env
os.environ["LOCAL_API_TOKEN"] = ""

# Prefer deterministic exchange defaults in unit tests unless overridden
os.environ.setdefault("EXCHANGE", "mexc")
os.environ.setdefault("HL_TESTNET", "true")
os.environ.setdefault("TRADING_ENABLED", "false")

try:
    from app.config import get_settings

    get_settings.cache_clear()
except Exception:
    pass

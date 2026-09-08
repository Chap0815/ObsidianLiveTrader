"""Publication checks must reject private paths without rejecting source assets."""
import pytest

from scripts.check_publication import forbidden_path


@pytest.mark.parametrize("path", [
    ".env", ".env.production", "config/.env", "config/.env.example", "data/trader.db",
    "private/api.key", "backup/trader.sqlite3", "runtime/trader.log",
    "runtime/trader.db-wal", ".superpowers/ui-review/account.png",
    "before-publication.bundle", "NEW_SESSION_PROMPT.md", ".venv/Scripts/python.exe",
    "../private.txt", "/private.txt", "private\\api.pem",
    "docs/HARDENING_LOG.md",
    "docs/AUDIT_2026-09-06.md", "docs/superpowers/plans/old.md", "PROJECT.md", "AGENDA.md",
])
def test_private_paths_are_blocked(path):
    assert forbidden_path(path)


@pytest.mark.parametrize("path", [
    ".env.example", "app/config.py", "docs/media/dashboard.png",
    "tests/fixtures/ohlcv_sample.json", ".github/workflows/checks.yml",
    "LICENSES/IBM-Plex-OFL-1.1.txt",
])
def test_public_assets_are_allowed(path):
    assert not forbidden_path(path)

"""Dependency-boundary regression tests."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGES = {"pytest", "pytest-asyncio"}


def _requirement_names(filename: str) -> set[str]:
    names: set[str] = set()
    for raw in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.add(re.split(r"[<>=!~;\[]", line, maxsplit=1)[0].lower())
    return names


def test_test_framework_is_dev_only():
    runtime = _requirement_names("requirements.txt") | _requirement_names(
        "requirements.lock"
    )
    dev = _requirement_names("requirements-dev.txt")

    assert TEST_PACKAGES.isdisjoint(runtime)
    assert TEST_PACKAGES <= dev

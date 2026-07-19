"""W3-Setup/Builder config-safety (Task 45).

Covers the two hardened invariants:
- W3-02: copying ``.env.example`` must NOT re-open the unauthenticated setup
  window (whose save OVERWRITES the entire real ``.env``).
- W3-07/W3-09: setup.html defaults have ONE source of truth on the server —
  model ids from ``DEFAULT_MODELS`` and the risk-profile card texts from
  ``RISK_PROFILES`` (so a displayed RRR/cap can never drift).
"""

from __future__ import annotations


def test_env_example_copy_does_not_reopen_setup(tmp_path, monkeypatch):
    """A straight copy of ``.env.example`` is treated as setup-complete.

    The unauth ``POST /api/setup`` save rewrites the whole ``.env``; an example
    copy that re-triggers setup would be a data-loss/security footgun. The
    example ships ``SETUP_COMPLETE=true`` as an ACTIVE line, and the hardened
    ``_setup_needed()`` also skips setup when real exchange keys are present.
    """
    from app.config import Settings
    from app.exchange_factory import exchange_ready
    from app.main import ENV_PATH

    for var in (
        "SETUP_COMPLETE",
        "EXCHANGE",
        "HL_TESTNET",
        "HL_PRIVATE_KEY",
        "MEXC_API_KEY",
        "MEXC_API_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)

    example = (ENV_PATH.parent / ".env.example").read_text(encoding="utf-8")
    # ACTIVE marker line — not merely a commented "# SETUP_COMPLETE=true".
    assert any(
        line.strip() == "SETUP_COMPLETE=true" for line in example.splitlines()
    ), "SETUP_COMPLETE=true must be an active line in .env.example (W3-02)"

    p = tmp_path / ".env"
    p.write_text(example, encoding="utf-8")
    s = Settings(_env_file=str(p))
    assert s.setup_complete is True
    # Mirrors the hardened _setup_needed() decision: complete marker OR ready keys.
    assert (s.setup_complete or exchange_ready(s)) is True


def test_setup_defaults_come_from_server():
    """setup.html model + risk texts render from the server, not JS duplicates."""
    from app.config import RISK_PROFILES
    from app.env_builder import DEFAULT_MODELS
    from app.main import _setup_template_context, templates

    ctx = _setup_template_context()
    assert ctx["default_models"] == DEFAULT_MODELS

    html = templates.env.get_template("setup.html").render(csp_nonce="n", **ctx)

    # ONE model truth: the server default id, never the stale hardcoded opus id.
    assert DEFAULT_MODELS["claude"] == "claude-sonnet-5"
    assert "claude-opus-4-8" not in html
    assert DEFAULT_MODELS["claude"] in html

    # Risk-card texts are generated from RISK_PROFILES — the balanced card must
    # show the REAL min_rrr (1.5), not the previously hardcoded lie ("RRR 2.0").
    assert float(RISK_PROFILES["balanced"]["min_rrr"]) == 1.5
    assert "RRR 1.5" in html

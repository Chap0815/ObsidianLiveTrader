import math
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

MEXC_API_BASE_URL = "https://api.mexc.com"
MEXC_ALLOWED_HOSTS = frozenset({"api.mexc.com"})
MEXC_LEGACY_HOST = "contract.mexc.com"

HL_ALLOWED_HOSTS = frozenset(
    {
        "api.hyperliquid.xyz",
        "api.hyperliquid-testnet.xyz",
    }
)

# LLM HTTPS API hosts only (SSRF: no arbitrary base URLs)
ANTHROPIC_ALLOWED_HOSTS = frozenset({"api.anthropic.com"})
XAI_ALLOWED_HOSTS = frozenset({"api.x.ai"})
OPENAI_ALLOWED_HOSTS = frozenset({"api.openai.com"})

_HL_PRIVATE_KEY_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_HL_ACCOUNT_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_SECP256K1_ORDER = int(
    "fffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141", 16
)


def is_valid_hl_private_key(value: str) -> bool:
    """Return whether value encodes a valid secp256k1 private scalar."""
    normalized = (value or "").strip()
    return bool(
        _HL_PRIVATE_KEY_RE.fullmatch(normalized)
        and 0 < int(normalized[2:], 16) < _SECP256K1_ORDER
    )

# Risk presets. RISK_PROFILE picks a preset; any value explicitly set in .env
# always wins over the preset (only unset fields are filled from it). The
# preset tunes the COMFORT limits (risk %, leverage ceiling, RRR strictness,
# equity-relative size cap) — never the essential fail-closed guards
# (equity-unknown, arming, exchange filters, one-time token, no LLM bypass).
RISK_PROFILES: dict[str, dict[str, object]] = {
    # Tight: small risk, low leverage, RRR enforced, size close to equity.
    "conservative": dict(
        max_risk_pct=1.0,
        max_leverage=20,
        max_notional_pct_of_equity=1000.0,  # 10× equity
        min_rrr=2.0,
        strict_rrr=True,
        strict_available_margin=True,
    ),
    # Default: freer size, moderate risk, RRR as warning only. Deliberately
    # does NOT set max_price_drift_pct — that field's own default (1.0) IS
    # the balanced value, so a config read never lies about what applies.
    "balanced": dict(
        max_risk_pct=5.0,
        max_leverage=50,
        max_notional_pct_of_equity=5000.0,  # 50× equity — only catches fat-finger
        min_rrr=1.5,
        strict_rrr=False,
        strict_available_margin=True,
    ),
    # Free: user decides size, high risk ceiling, no equity size cap.
    "free": dict(
        max_risk_pct=15.0,
        max_leverage=100,
        max_notional_pct_of_equity=0.0,  # 0 = off
        min_rrr=1.5,
        strict_rrr=False,
        strict_available_margin=False,
    ),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8-sig",
        extra="ignore",
        populate_by_name=True,
    )

    # First-run marker. launch.py writes a bootstrap .env with
    # SETUP_COMPLETE=false so /setup stays open (no secrets exist yet); the
    # full save flips it true and locks /setup. Fail-safe default False.
    setup_complete: bool = False

    host: str = "127.0.0.1"
    port: int = 8787

    # mexc | hyperliquid
    exchange: str = "hyperliquid"

    mexc_api_key: str = ""
    mexc_api_secret: str = ""
    mexc_base_url: str = MEXC_API_BASE_URL

    # Hyperliquid (agent/API wallet private key 0x… — cannot withdraw)
    hl_private_key: str = ""
    hl_account_address: str = ""  # main wallet address if using agent key
    hl_testnet: bool = True
    # W3-01: explicit one-time acknowledgement for the testnet→mainnet switch.
    # Switching to real money is otherwise a silent HL_TESTNET=false while
    # TRADING_ENABLED=true from the testnet phase stays armed. Fail-closed
    # default False: an armed Hyperliquid mainnet start is refused until the
    # user sets MAINNET_ACK=true (enforced in the lifespan startup, not here,
    # so Settings() stays constructible for disarmed/inspection use). MEXC has
    # no testnet, so this gate is a no-op there.
    mainnet_ack: bool = False
    hl_base_url: str = ""  # empty → SDK default from testnet flag
    # Q-01: hard per-request timeout (seconds) for every Hyperliquid SDK HTTP
    # call. The SDK defaults to NO timeout, so a hung endpoint would block a
    # worker forever; this bounds every call. Finite, 0.5..120s.
    hl_http_timeout_s: float = 10.0

    # claude | xai | openai (Codex) | ollama (lokal)  — default: Claude
    llm_provider: str = "claude"

    # Anthropic Claude (env: ANTHROPIC_API_KEY or CLAUDE_API_KEY)
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "anthropic_api_key",
            "ANTHROPIC_API_KEY",
            "CLAUDE_API_KEY",
            "claude_api_key",
        ),
    )
    anthropic_model: str = "claude-sonnet-5"
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_version: str = "2023-06-01"

    # Optional xAI Grok (if LLM_PROVIDER=xai); model id via .env anpassbar
    xai_api_key: str = ""
    xai_model: str = "grok-4"
    xai_base_url: str = "https://api.x.ai/v1"

    # Optional OpenAI Codex (if LLM_PROVIDER=openai)
    openai_api_key: str = ""
    openai_model: str = "gpt-5.1"
    openai_base_url: str = "https://api.openai.com/v1"

    # Optional lokales Ollama (if LLM_PROVIDER=ollama) — kein Key nötig
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str = "llama3.1"

    # Markt-Scanner: billiges/schnelles Modell screent viele Coins,
    # die Detail-Analyse pro Coin läuft weiter über LLM_PROVIDER (z.B. Opus).
    scanner_model: str = "claude-sonnet-5"
    scanner_max_coins: int = 20
    # Task 24 (S2-01/S2-08/S2-02) — Scanner-Redesign, reversibel per SCANNER_MODE.
    #   classic   = altes Verhalten: Top-N-nach-Turnover in EINEM LLM-Call.
    #   prefilter = Multi-Ranking-Universum (Momentum/Flow + Turnover-Floor),
    #               deterministischer Rules-Prefilter -> nur Top-K ans LLM,
    #               Chunk-Split+Merge bei >chunk_max Kandidaten.
    # Default prefilter; classic bleibt als Fallback byte-genau erhalten.
    scanner_mode: str = "prefilter"
    # Kandidaten aus market_overview im prefilter-Mode (Universum vor Klines).
    scanner_universe_size: int = 75
    # Top-N je Ranking-Dimension (|price-change|, |OI-Δ|, Volatilität) für die Union.
    scanner_rank_top_n: int = 35
    # 24h-Turnover-Liquiditätsfloor (USD): unter diesem Wert fliegt ein Coin
    # aus dem Universum, egal wie stark er sich bewegt (Wash-/Illiquid-Schutz).
    scanner_turnover_floor_usd: float = 5_000_000.0
    # Stufe-1-Vorfilter (billig, OHNE Klines): im prefilter-Mode bekommen nur so
    # viele rang-sortierte Universum-Coins ueberhaupt Klines gefetcht, BEVOR der
    # klines-basierte Stufe-2-Prefilter laeuft. Kappt den 429-treibenden Klines-
    # Sturm (~universe_size×3 Calls). >= scanner_universe_size deaktiviert die
    # Kuerzung; nie unter scanner_prefilter_top_k (Stufe-2 braucht Auswahl).
    scanner_prefilter_stage1_k: int = 40
    # Deterministischer Prefilter reicht nur so viele Coins ans (teure) LLM weiter.
    scanner_prefilter_top_k: int = 15
    # >so viele LLM-Kandidaten -> in 2 Chunks splitten und mergen (S2-02). Nur im
    # prefilter-Mode aktiv; classic bleibt IMMER ein einziger Call (byte-genau).
    scanner_llm_chunk_max: int = 12

    # ── Hyperliquid Read-Rate-Budget (proaktiv gegen 429) ────────────────────
    # Der Scanner-Fan-out (bis ~universe_size×3 Klines-Calls je Scan) plus die
    # Frontend-/Monitor-Polls teilen sich EINEN Token-Bucket: Read-Calls werden
    # auf hl_read_max_rps/s gedrosselt (Kurzburst bis hl_read_burst), damit die
    # per-IP-Rate von Hyperliquid nicht in 429 → /api/market 502 läuft.
    # Der Money-Pfad (Order/Modify/Cancel) ist NIE gedrosselt. 0 = Drossel aus.
    # Höher = schnellere Scans, aber näher an Hyperliquids Limit (~20/s).
    hl_read_max_rps: float = 10.0
    hl_read_burst: float = 20.0
    # Konservativ | Ausgewogen(balanced) | Frei(free). Presets fill only fields
    # NOT explicitly set in .env — see RISK_PROFILES above. These field defaults
    # MIRROR the default "balanced" preset so a config read never lies about the
    # limits that actually apply when RISK_PROFILE is left at its default.
    risk_profile: str = "balanced"
    max_leverage: int = 50
    max_risk_pct: float = 5.0
    min_rrr: float = 1.5
    strict_rrr: bool = False
    # Equity-relative position-size cap (hard): notional must stay under
    # equity × pct/100. 0 = off. Scales with the account (unlike a fixed USDT
    # cap) and stays fail-closed to known equity. Fat-finger guard, not a
    # day-to-day brake (margin + risk-% bind long before this on real trades).
    max_notional_pct_of_equity: float = 5000.0
    default_symbol: str = "BTC"
    preview_token_ttl_seconds: int = 60
    database_path: str = "data/trader.db"
    trading_enabled: bool = False
    allow_unprotected_entry: bool = False
    # Manual trigger_mode places an entry WITHOUT an exchange-side SL/TP (the
    # trader manages the exit). Fail-closed default False so a hand-written or
    # incomplete .env cannot silently allow a naked manual entry; set True in
    # .env to opt in.
    allow_manual_trigger: bool = False
    risk_slippage_pct: float = 0.05
    # WARNING threshold only — NOT a hard cap. A larger order still passes as
    # long as the equity-relative cap (max_notional_pct_of_equity, the real
    # hard notional limit) and the other gates allow it. See the "Max
    # notional" section in risk/gates.py. 0 = off.
    max_notional_usdt: float = Field(
        default=500.0,
        description=(
            "Warning threshold (USDT) for order notional — NOT a hard cap. "
            "The hard, equity-scaled notional limit is "
            "max_notional_pct_of_equity. 0 = off."
        ),
    )
    max_price_drift_pct: float = 1.0
    market_entry_slippage_pct: float = 0.15
    allow_cross_margin: bool = False
    auto_flatten_if_sl_unverified: bool = True
    # SL-verify after placing: exchanges reflect a fresh trigger with a short
    # delay, so poll a few times before judging the SL missing (which would
    # otherwise auto-flatten a genuinely protected trade).
    sl_verify_attempts: int = 3
    sl_verify_delay_s: float = 0.7
    # Post-close position re-read: Hyperliquid may not reflect a fill the instant
    # we re-read, which can mislabel a clean full close as "partial". Re-read a
    # few times with a short settle delay. Defaults preserve the old single-read
    # behaviour (1 attempt, no delay) so nothing changes unless configured.
    close_verify_attempts: int = 1
    close_verify_delay_s: float = 0.0
    # F-15 (privacy): opt-IN, not opt-out. When true, equity, available
    # margin and the full open-positions list are sent to the external LLM
    # provider as part of the analysis context (see build_llm_context() in
    # app/llm/client.py). Defaults to false so no account data leaves the
    # machine unless the user explicitly sets INCLUDE_ACCOUNT_IN_LLM=true.
    include_account_in_llm: bool = False
    kline_limit_hint: int = 500
    local_api_token: str = ""
    require_loopback_when_armed: bool = True
    strict_available_margin: bool = True

    # Journal + feedback-loop (KI shadow book). Advisory/measurement only —
    # these never affect the order/gate/confirm path. Safe defaults so an
    # unchanged .env works.
    journal_enabled: bool = True
    journal_resolve_interval_s: int = 60
    journal_window_hours: int = 24
    journal_min_sample: int = 20

    # Trade-Management-Layer: break-even + time-stop monitor. Defensive
    # defaults so the monitor/rules can `getattr` these safely; validators
    # reject NaN/Inf/out-of-bounds so gate comparisons never fail-open.
    tm_enabled: bool = True
    tm_monitor_interval_s: int = 20
    tm_be_trigger_r: float = 1.0
    tm_be_fee_rt: float = 0.0006
    tm_time_stop_hours: float = 4.0
    tm_time_stop_min_r: float = 0.5

    # Auto-Trailing (ATR/Chandelier). Same defensive posture as the tm_*
    # fields above: validators reject NaN/Inf/out-of-bounds/unknown so gate
    # comparisons can never fail-open.
    tm_trail_atr_mult: float = 2.0
    tm_trail_activation_r: float = 1.0
    tm_trail_atr_period: int = 14
    tm_trail_atr_tf: str = "15m"
    # F3: minimum trail step as a fraction of ATR — the trail only emits a new
    # modify_stop_loss when it beats the live SL by at least this*ATR, so a slow
    # trend no longer triggers a full exchange round-trip every cycle. 0 = old
    # any-improvement behavior. Same defensive posture (finite, >= 0).
    tm_trail_min_step_atr: float = 0.25

    # Block 2/TP2 Task P2: minimum resolved sample a confidence group needs
    # before the server-side Confidence-Recalibration may downgrade its DISPLAY
    # + shrink its sizing SUGGESTION. Advisory/measurement only — NEVER blocks,
    # vetoes, forces STAY_OUT, or tightens a gate. Below this n the raw KI tier
    # is shown unchanged (no overfit on a tiny sample).
    tm_recal_min_sample: int = 20

    @model_validator(mode="after")
    def _apply_risk_profile(self):
        """Fill preset values for any risk field NOT explicitly set in .env.
        Explicit .env values are in model_fields_set and always win."""
        preset = RISK_PROFILES.get((self.risk_profile or "").strip().lower())
        if preset:
            for key, value in preset.items():
                if key not in self.model_fields_set:
                    setattr(self, key, value)
        return self

    @field_validator("host")
    @classmethod
    def host_loopback_only(cls, v: str) -> str:
        allowed = {"127.0.0.1", "localhost", "::1"}
        if v not in allowed:
            raise ValueError(
                f"HOST must be one of {sorted(allowed)} (got {v!r}). "
                "LAN bind is forbidden for this trading app."
            )
        return v

    @field_validator("port")
    @classmethod
    def port_ok(cls, v: int) -> int:
        if not (1024 <= int(v) <= 65_535):
            raise ValueError(
                f"PORT must be an integer in [1024, 65535] (got {v!r})"
            )
        return int(v)

    @field_validator("exchange")
    @classmethod
    def exchange_ok(cls, v: str) -> str:
        x = (v or "").strip().lower()
        if x in ("hl", "hyperliquid"):
            return "hyperliquid"
        if x == "mexc":
            return "mexc"
        raise ValueError("EXCHANGE must be 'mexc' or 'hyperliquid'")

    @field_validator(
        "anthropic_model",
        "xai_model",
        "openai_model",
        "ollama_model",
        "scanner_model",
    )
    @classmethod
    def llm_model_trimmed(cls, v: str) -> str:
        return (v or "").strip()

    @field_validator("scanner_mode")
    @classmethod
    def scanner_mode_ok(cls, v: str) -> str:
        """Normalize to classic|prefilter. Unknown values fail closed to the
        safe fallback 'classic' so a typo never silently enables the new path."""
        x = (v or "prefilter").strip().lower()
        if x in ("classic", "prefilter"):
            return x
        return "classic"

    @field_validator(
        "scanner_universe_size",
        "scanner_rank_top_n",
        "scanner_prefilter_top_k",
        "scanner_prefilter_stage1_k",
    )
    @classmethod
    def scanner_positive_int_ok(cls, v: int, info) -> int:
        if not (1 <= int(v) <= 500):
            raise ValueError(
                f"{info.field_name.upper()} must be an integer in [1, 500] (got {v!r})"
            )
        return int(v)

    @field_validator("hl_read_max_rps")
    @classmethod
    def hl_read_max_rps_ok(cls, v: float) -> float:
        # inf would pass a bare `v < 0` check and silently disable the limiter
        # (max(1e-6, inf) == inf → never throttle); require a finite value.
        if not math.isfinite(v) or v < 0:
            raise ValueError(
                "HL_READ_MAX_RPS must be a finite number >= 0 "
                f"(0 disables the read-rate limiter) (got {v!r})"
            )
        return v

    @field_validator("hl_read_burst")
    @classmethod
    def hl_read_burst_ok(cls, v: float) -> float:
        if not math.isfinite(v) or v < 1:
            raise ValueError(
                f"HL_READ_BURST must be a finite number >= 1 (got {v!r})"
            )
        return v

    @field_validator("scanner_llm_chunk_max")
    @classmethod
    def scanner_llm_chunk_max_ok(cls, v: int) -> int:
        if not (2 <= int(v) <= 100):
            raise ValueError(
                f"SCANNER_LLM_CHUNK_MAX must be an integer in [2, 100] (got {v!r})"
            )
        return int(v)

    @field_validator("scanner_turnover_floor_usd")
    @classmethod
    def scanner_turnover_floor_ok(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError(
                "SCANNER_TURNOVER_FLOOR_USD must be a finite number >= 0 "
                f"(0 = off) (got {v!r})"
            )
        return v

    @field_validator("mexc_base_url")
    @classmethod
    def mexc_url_https_allowlist(cls, v: str) -> str:
        raw = (v or "").strip().rstrip("/")
        if not raw:
            return MEXC_API_BASE_URL
        parsed = urlparse(raw)
        if parsed.scheme != "https":
            raise ValueError("MEXC_BASE_URL must use https://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("MEXC_BASE_URL must not include credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("MEXC_BASE_URL must not include a query or fragment")
        if parsed.path:
            raise ValueError("MEXC_BASE_URL must not include a path")
        if parsed.port not in (None, 443):
            raise ValueError("MEXC_BASE_URL must use the default HTTPS port")
        host = (parsed.hostname or "").lower()
        if host == MEXC_LEGACY_HOST:
            return MEXC_API_BASE_URL
        if host not in MEXC_ALLOWED_HOSTS:
            raise ValueError(
                f"MEXC_BASE_URL host {host!r} not in allowlist "
                f"{sorted(MEXC_ALLOWED_HOSTS)}"
            )
        return raw

    @field_validator("hl_base_url")
    @classmethod
    def hl_url_ok(cls, v: str) -> str:
        raw = (v or "").strip().rstrip("/")
        if not raw:
            return ""
        parsed = urlparse(raw)
        if parsed.scheme != "https":
            raise ValueError("HL_BASE_URL must use https://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("HL_BASE_URL must not include credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("HL_BASE_URL must not include a query or fragment")
        if parsed.path:
            raise ValueError("HL_BASE_URL must not include a path")
        if parsed.port not in (None, 443):
            raise ValueError("HL_BASE_URL must use the default HTTPS port")
        host = (parsed.hostname or "").lower()
        if host not in HL_ALLOWED_HOSTS:
            raise ValueError(
                f"HL_BASE_URL host {host!r} not in allowlist "
                f"{sorted(HL_ALLOWED_HOSTS)}"
            )
        return raw

    @model_validator(mode="after")
    def hl_network_matches_base_url(self) -> "Settings":
        """Reject contradictory Hyperliquid network and endpoint settings."""
        if not self.hl_base_url:
            return self
        host = (urlparse(self.hl_base_url).hostname or "").lower()
        expected = (
            "api.hyperliquid-testnet.xyz"
            if self.hl_testnet
            else "api.hyperliquid.xyz"
        )
        if host != expected:
            network = "testnet" if self.hl_testnet else "mainnet"
            raise ValueError(
                f"HL_TESTNET selects {network}, but HL_BASE_URL points to "
                f"{host!r}; expected {expected!r}. Refusing an ambiguous "
                "Hyperliquid network configuration."
            )
        return self

    @field_validator("ollama_base_url")
    @classmethod
    def ollama_url_loopback_only(cls, v: str) -> str:
        """Ollama is local-only — block SSRF via a remote OLLAMA_BASE_URL."""
        raw = (v or "").strip().rstrip("/") or "http://127.0.0.1:11434/v1"
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("OLLAMA_BASE_URL must use http:// or https://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("OLLAMA_BASE_URL must not include credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("OLLAMA_BASE_URL must not include a query or fragment")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("OLLAMA_BASE_URL must include a valid port") from exc
        host = (parsed.hostname or "").lower()
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"OLLAMA_BASE_URL host {host!r} must be loopback "
                "(127.0.0.1 / localhost / ::1) to prevent SSRF"
            )
        return raw

    @staticmethod
    def _https_host_allowlist(
        raw: str,
        *,
        name: str,
        allowed: frozenset[str],
        allowed_paths: frozenset[str],
    ) -> str:
        v = (raw or "").strip().rstrip("/")
        if not v:
            raise ValueError(f"{name} must not be empty")
        parsed = urlparse(v)
        if parsed.scheme != "https":
            raise ValueError(f"{name} must use https://")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"{name} must not include credentials")
        if parsed.query or parsed.fragment:
            raise ValueError(f"{name} must not include query parameters or fragments")
        if parsed.path not in allowed_paths:
            raise ValueError(f"{name} has an unsupported API path")
        if parsed.port not in (None, 443):
            raise ValueError(f"{name} must use the default HTTPS port")
        host = (parsed.hostname or "").lower()
        if host not in allowed:
            raise ValueError(
                f"{name} host {host!r} not in allowlist {sorted(allowed)}"
            )
        return v

    @field_validator("anthropic_base_url")
    @classmethod
    def anthropic_url_allowlist(cls, v: str) -> str:
        return cls._https_host_allowlist(
            v or "https://api.anthropic.com",
            name="ANTHROPIC_BASE_URL",
            allowed=ANTHROPIC_ALLOWED_HOSTS,
            allowed_paths=frozenset({""}),
        )

    @field_validator("xai_base_url")
    @classmethod
    def xai_url_allowlist(cls, v: str) -> str:
        return cls._https_host_allowlist(
            v or "https://api.x.ai/v1",
            name="XAI_BASE_URL",
            allowed=XAI_ALLOWED_HOSTS,
            allowed_paths=frozenset({"/v1"}),
        )

    @field_validator("openai_base_url")
    @classmethod
    def openai_url_allowlist(cls, v: str) -> str:
        return cls._https_host_allowlist(
            v or "https://api.openai.com/v1",
            name="OPENAI_BASE_URL",
            allowed=OPENAI_ALLOWED_HOSTS,
            allowed_paths=frozenset({"/v1"}),
        )

    # ── F-06: money/risk floats must be finite and within sane bounds. NaN
    # or Infinity would make gate comparisons (e.g. `risk_pct > max_risk_pct`)
    # silently False, so a gate could fail OPEN instead of blocking. ─────────

    @field_validator("max_risk_pct")
    @classmethod
    def max_risk_pct_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0 < v <= 100):
            raise ValueError(
                f"MAX_RISK_PCT must be a finite number in (0, 100] (got {v!r})"
            )
        return v

    @field_validator("max_leverage")
    @classmethod
    def max_leverage_ok(cls, v: int) -> int:
        if not (1 <= v <= 500):
            raise ValueError(
                f"MAX_LEVERAGE must be an integer in [1, 500] (got {v!r})"
            )
        return v

    @field_validator("min_rrr")
    @classmethod
    def min_rrr_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0 <= v <= 1000):
            raise ValueError(
                f"MIN_RRR must be a finite number in [0, 1000] (got {v!r})"
            )
        return v

    @field_validator("max_notional_pct_of_equity")
    @classmethod
    def max_notional_pct_of_equity_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0 <= v <= 1_000_000):
            raise ValueError(
                "MAX_NOTIONAL_PCT_OF_EQUITY must be a finite number in "
                f"[0, 1000000] (0 = off) (got {v!r})"
            )
        return v

    @field_validator("max_notional_usdt")
    @classmethod
    def max_notional_usdt_ok(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError(
                "MAX_NOTIONAL_USDT must be a finite number >= 0 (0 = off; "
                f"warning threshold, not a hard cap) (got {v!r})"
            )
        return v

    @field_validator(
        "risk_slippage_pct", "max_price_drift_pct", "market_entry_slippage_pct"
    )
    @classmethod
    def slippage_pct_ok(cls, v: float, info) -> float:
        if not math.isfinite(v) or not (0 <= v <= 100):
            raise ValueError(
                f"{info.field_name.upper()} must be a finite number in "
                f"[0, 100] (got {v!r})"
            )
        return v

    @field_validator("hl_http_timeout_s")
    @classmethod
    def hl_http_timeout_s_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0.5 <= v <= 120):
            raise ValueError(
                f"HL_HTTP_TIMEOUT_S must be a finite number in [0.5, 120] "
                f"(got {v!r})"
            )
        return v

    @field_validator("sl_verify_delay_s")
    @classmethod
    def sl_verify_delay_s_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0 <= v <= 60):
            raise ValueError(
                f"SL_VERIFY_DELAY_S must be a finite number in [0, 60] "
                f"(got {v!r})"
            )
        return v

    @field_validator("close_verify_delay_s")
    @classmethod
    def close_verify_delay_s_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0 <= v <= 60):
            raise ValueError(
                f"CLOSE_VERIFY_DELAY_S must be a finite number in [0, 60] "
                f"(got {v!r})"
            )
        return v

    @field_validator("kline_limit_hint")
    @classmethod
    def kline_limit_hint_ok(cls, v: int) -> int:
        if not (10 <= int(v) <= 1500):
            raise ValueError(
                f"KLINE_LIMIT_HINT must be an integer in [10, 1500] (got {v!r})"
            )
        return int(v)

    @field_validator("preview_token_ttl_seconds")
    @classmethod
    def preview_token_ttl_seconds_ok(cls, v: int) -> int:
        if not (1 <= int(v) <= 3600):
            raise ValueError(
                f"PREVIEW_TOKEN_TTL_SECONDS must be an integer in [1, 3600] "
                f"(got {v!r})"
            )
        return int(v)

    @field_validator("tm_monitor_interval_s")
    @classmethod
    def tm_monitor_interval_s_ok(cls, v: int) -> int:
        if not (5 <= int(v) <= 300):
            raise ValueError(
                f"TM_MONITOR_INTERVAL_S must be an integer in [5, 300] (got {v!r})"
            )
        return int(v)

    @field_validator("tm_be_trigger_r")
    @classmethod
    def tm_be_trigger_r_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0.1 <= v <= 10):
            raise ValueError(
                f"TM_BE_TRIGGER_R must be a finite number in [0.1, 10] (got {v!r})"
            )
        return v

    @field_validator("tm_be_fee_rt")
    @classmethod
    def tm_be_fee_rt_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0 <= v <= 0.01):
            raise ValueError(
                f"TM_BE_FEE_RT must be a finite number in [0, 0.01] (got {v!r})"
            )
        return v

    @field_validator("tm_time_stop_hours")
    @classmethod
    def tm_time_stop_hours_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0.25 <= v <= 168):
            raise ValueError(
                f"TM_TIME_STOP_HOURS must be a finite number in [0.25, 168] "
                f"(got {v!r})"
            )
        return v

    @field_validator("tm_time_stop_min_r")
    @classmethod
    def tm_time_stop_min_r_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (-5 <= v <= 10):
            raise ValueError(
                f"TM_TIME_STOP_MIN_R must be a finite number in [-5, 10] "
                f"(got {v!r})"
            )
        return v

    @field_validator("tm_trail_atr_mult")
    @classmethod
    def tm_trail_atr_mult_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0.5 <= v <= 10):
            raise ValueError(
                f"TM_TRAIL_ATR_MULT must be a finite number in [0.5, 10] "
                f"(got {v!r})"
            )
        return v

    @field_validator("tm_trail_activation_r")
    @classmethod
    def tm_trail_activation_r_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0 <= v <= 10):
            raise ValueError(
                f"TM_TRAIL_ACTIVATION_R must be a finite number in [0, 10] "
                f"(got {v!r})"
            )
        return v

    @field_validator("tm_trail_min_step_atr")
    @classmethod
    def tm_trail_min_step_atr_ok(cls, v: float) -> float:
        if not math.isfinite(v) or not (0 <= v <= 10):
            raise ValueError(
                f"TM_TRAIL_MIN_STEP_ATR must be a finite number in [0, 10] "
                f"(got {v!r})"
            )
        return v

    @field_validator("tm_trail_atr_period")
    @classmethod
    def tm_trail_atr_period_ok(cls, v: int) -> int:
        if not (2 <= int(v) <= 100):
            raise ValueError(
                f"TM_TRAIL_ATR_PERIOD must be an integer in [2, 100] (got {v!r})"
            )
        return int(v)

    @field_validator("tm_recal_min_sample")
    @classmethod
    def tm_recal_min_sample_ok(cls, v: int) -> int:
        if not (1 <= int(v) <= 1000):
            raise ValueError(
                f"TM_RECAL_MIN_SAMPLE must be an integer in [1, 1000] (got {v!r})"
            )
        return int(v)

    @field_validator("tm_trail_atr_tf")
    @classmethod
    def tm_trail_atr_tf_ok(cls, v: str) -> str:
        allowed = {"5m", "15m", "1h", "4h"}
        x = (v or "").strip().lower()
        if x not in allowed:
            raise ValueError(
                f"TM_TRAIL_ATR_TF must be one of {sorted(allowed)} (got {v!r})"
            )
        return x

    @model_validator(mode="after")
    def armed_requires_local_token(self) -> "Settings":
        """Live trading requires a non-empty LOCAL_API_TOKEN.

        An empty token leaves the local API unauthenticated; combined with
        TRADING_ENABLED=true that is an open door to place real orders. Fail
        closed at construction time with a clear message.
        """
        if self.trading_enabled and not (self.local_api_token or "").strip():
            raise ValueError(
                "TRADING_ENABLED=true requires LOCAL_API_TOKEN to be set "
                "(empty token = unauthenticated local API). "
                "Set LOCAL_API_TOKEN in .env to arm live trading."
            )
        return self

    @model_validator(mode="after")
    def armed_requires_loopback(self) -> "Settings":
        if (
            self.trading_enabled
            and self.require_loopback_when_armed
            and self.host not in ("127.0.0.1", "localhost", "::1")
        ):
            raise ValueError(
                "TRADING_ENABLED=true requires loopback HOST (127.0.0.1/localhost)"
            )
        return self

    @property
    def mexc_ready(self) -> bool:
        return bool(
            (self.mexc_api_key or "").strip()
            and (self.mexc_api_secret or "").strip()
        )

    @property
    def hl_ready(self) -> bool:
        private_key = (self.hl_private_key or "").strip()
        account_address = (self.hl_account_address or "").strip()
        return bool(
            is_valid_hl_private_key(private_key)
            and (
                not account_address
                or _HL_ACCOUNT_ADDRESS_RE.fullmatch(account_address)
            )
        )

    @property
    def exchange_ready(self) -> bool:
        if self.exchange == "hyperliquid":
            return self.hl_ready
        if self.exchange == "mexc":
            return self.mexc_ready
        return False

    @property
    def xai_ready(self) -> bool:
        return bool(
            (self.xai_api_key or "").strip()
            and (self.xai_model or "").strip()
        )

    @property
    def claude_ready(self) -> bool:
        return bool(
            (self.anthropic_api_key or "").strip()
            and (self.anthropic_model or "").strip()
        )

    @property
    def openai_ready(self) -> bool:
        return bool(
            (self.openai_api_key or "").strip()
            and (self.openai_model or "").strip()
        )

    @property
    def ollama_ready(self) -> bool:
        # Local server, no key — reachable-or-not shows up at call time
        return bool(
            (self.ollama_base_url or "").strip()
            and (self.ollama_model or "").strip()
        )

    @property
    def llm_ready(self) -> bool:
        p = (self.llm_provider or "claude").strip().lower()
        if p in ("claude", "anthropic"):
            return self.claude_ready
        if p in ("xai", "grok"):
            return self.xai_ready
        if p in ("openai", "codex"):
            return self.openai_ready
        if p in ("ollama", "local"):
            return self.ollama_ready
        return False

    @property
    def resolved_llm_provider(self) -> str:
        """The configured LLM_PROVIDER if it actually has a key, otherwise the
        first provider that IS configured. Prevents the hardcoded 'claude'
        default from failing the analysis when Anthropic has no key but another
        provider (Grok/Codex/Ollama) is set up. Returns the configured one
        unchanged when NONE are ready, so the downstream error stays honest."""
        ready = {
            "claude": self.claude_ready,
            "xai": self.xai_ready,
            "openai": self.openai_ready,
            "ollama": self.ollama_ready,
        }
        aliases = {"anthropic": "claude", "grok": "xai", "codex": "openai", "local": "ollama"}
        cur = (self.llm_provider or "claude").strip().lower()
        cur = aliases.get(cur, cur)
        if ready.get(cur):
            return cur
        # Fall back ONLY to a provider that has a real key (claude/xai/openai).
        # Ollama is deliberately excluded from the fallback: its base_url always
        # defaults to loopback so ollama_ready is always True even with no local
        # server running — auto-jumping to it would mask a genuine "no LLM
        # configured" state. Ollama is still used when explicitly selected (cur).
        for p in ("claude", "xai", "openai"):
            if ready.get(p):
                return p
        return cur


@lru_cache
def get_settings() -> Settings:
    return Settings()

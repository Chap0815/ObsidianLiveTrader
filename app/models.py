from typing import Any, Literal

from pydantic import BaseModel, Field


class Candle(BaseModel):
    """OHLCV bar. `time` is milliseconds (chart-friendly)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    vol: float  # contract volume on MEXC futures
    amount: float = 0.0


class Ticker(BaseModel):
    symbol: str
    last_price: float
    bid1: float | None = None
    ask1: float | None = None
    fair_price: float | None = None
    index_price: float | None = None
    volume24: float | None = None
    amount24: float | None = None
    funding_rate: float | None = None
    timestamp: int | None = None


class FundingRate(BaseModel):
    symbol: str
    funding_rate: float
    max_funding_rate: float | None = None
    min_funding_rate: float | None = None
    collect_cycle: int | None = None
    next_settle_time: int | None = None
    timestamp: int | None = None


class ContractMeta(BaseModel):
    """Key contract filters for risk/sizing and apiAllowed gate."""

    symbol: str
    contract_size: float
    price_unit: float
    vol_unit: float
    min_vol: float
    max_vol: float
    max_leverage: int
    min_leverage: int = 1
    min_notional: float = 0.0  # exchange min order value in quote ccy (0 = none)
    api_allowed: bool = False
    price_scale: int | None = None
    vol_scale: int | None = None
    base_coin: str | None = None
    quote_coin: str | None = None
    state: int | None = None


class SwingPoint(BaseModel):
    index: int
    time: int
    price: float
    kind: Literal["high", "low"]


class SwingPoints(BaseModel):
    highs: list[SwingPoint] = Field(default_factory=list)
    lows: list[SwingPoint] = Field(default_factory=list)


class KeyLevels(BaseModel):
    support: float | None = None
    resistance: float | None = None
    range_high: float | None = None
    range_low: float | None = None
    last_price: float | None = None
    major_pools: list[SwingPoint] = Field(default_factory=list)
    swings: SwingPoints = Field(default_factory=SwingPoints)


class TimeframeSlice(BaseModel):
    """One TF block inside a market snapshot (candles + indicators + structure)."""

    tf: str
    candles: list[Candle] = Field(default_factory=list)
    indicators: dict[str, Any] = Field(default_factory=dict)
    structure: dict[str, Any] = Field(default_factory=dict)


class MarketSnapshot(BaseModel):
    """Full public market context for chart API and LLM."""

    symbol: str
    last_price: float
    funding: dict[str, Any] = Field(default_factory=dict)
    contract: dict[str, Any] = Field(default_factory=dict)
    ltf: TimeframeSlice
    htf: TimeframeSlice
    # Positioning extras (Hyperliquid meta ctx): open_interest, premium,
    # prev_day_px, oi_change_pct_1h/4h. Empty dict on exchanges without OI.
    market: dict[str, Any] = Field(default_factory=dict)
    # Daily REGIME anchor (ultra-compact in the LLM context). Optional so
    # existing MarketSnapshot(...) construction without daily still validates.
    daily: TimeframeSlice | None = None


# --- Grok Trade Proposal (design §7) ---
# Advisory only: never bypasses risk gates; user must apply → preview → confirm.

TrendLabel = Literal["bullish", "bearish", "ranging"]
TradeAction = Literal["STRONG_BUY", "BUY", "STAY_OUT", "SELL", "STRONG_SHORT"]
SetupConfidence = Literal["low", "medium", "high"]
# Intended holding horizon of the setup (P2-08).
TimeHorizon = Literal["scalp", "intraday", "swing"]


class Confluence(BaseModel):
    """One independent confluence supporting the trade side (P2-07).

    The array length IS the confluence count that feeds the DECISION block;
    an item with EMPTY evidence does not count. `type` is the evidence kind
    (regime / pattern / value_at_structure / momentum / funding / positioning),
    `evidence` names the concrete data (a price, a swing, a reading).
    """

    type: str = ""
    evidence: str = ""


class ProposalKeyLevels(BaseModel):
    immediate_support: float | None = None
    immediate_resistance: float | None = None
    major_liquidity_pools: list[float | str] = Field(default_factory=list)


class ProposalManagement(BaseModel):
    move_sl_to_be: str = ""
    early_invalidation: str = ""


class TradeProposal(BaseModel):
    """Validated Grok trade proposal. Numeric fields nullable on STAY_OUT."""

    htf_trend: TrendLabel
    ltf_trend: TrendLabel
    key_levels: ProposalKeyLevels = Field(default_factory=ProposalKeyLevels)
    # Named chart formation if clearly present in the data, else "none".
    chart_pattern: str = ""
    pattern_confidence: str = ""  # low | medium | high | ""
    volume_momentum: str = ""
    # Overall confidence in the setup (distinct from pattern_confidence). The
    # prompt DERIVES this from evidence (CONFIDENCE DERIVATION table, no
    # medium-anchoring, P2-05). The "medium" here is only a PARSE fallback so
    # older/loose LLM output that omits the field still validates — it is NOT a
    # modeling default (see app/llm/client.py parse_proposal normalization).
    setup_confidence: SetupConfidence = "medium"
    # Numeric conviction (0-10) the LLM DERIVES alongside setup_confidence
    # (P2-05, breaks the medium mode-collapse): 0-3 -> low, 4-6 -> medium,
    # 7-10 -> high. Optional/nullable so older output that omits it still
    # validates (see parse_proposal normalization).
    conviction_score: int | None = Field(None, ge=0, le=10)
    action: TradeAction
    trigger_entry_zone: str = ""
    entry_price: float | None = None
    tp1: float | None = None
    tp2: float | None = None
    tp3: float | None = None
    stop_loss: float | None = None
    rrr: float | None = None
    recommended_leverage: str = ""
    position_sizing_note: str = ""
    funding_alert: str = ""
    management: ProposalManagement = Field(default_factory=ProposalManagement)
    # Structured invalidation (optional; supplements management.early_invalidation
    # free text) so the app can later monitor it programmatically. Null/empty
    # when the LLM has no distinct level beyond the stop-loss itself.
    invalidation_price: float | None = None
    invalidation_tf: str = ""
    # Independent confluences the DECISION counted (P2-07). The array length
    # IS the count; an item with EMPTY evidence does not count. Default empty
    # so older proposals that omit it still parse.
    confluences: list[Confluence] = Field(default_factory=list)
    # Concise "if wrong, then" counter-thesis (P2-08). Null when none stated.
    alternative_scenario: str | None = None
    # Intended holding horizon (P2-08). Null when the LLM leaves it unset.
    time_horizon: TimeHorizon | None = None
    rationale: str = ""
    # Block 2/TP2 Task P3: the ONE most likely reason THIS specific trade
    # fails, named before entry (not a generic disclaimer). Purely additive/
    # advisory display field -- optional so a response that omits it (older
    # prompt version, model slip) still parses; never read by any gate,
    # sizing or decision path.
    pre_mortem: str | None = None


class AnalyzeRequest(BaseModel):
    symbol: str = "BTC_USDT"
    tf: str = "15m"
    htf: str = "1H"
    # Optional pre-screen from the market scanner for THIS coin (bias/setup/
    # key_level/score). Advisory only — the analyzer confirms or refutes it and
    # it is sanitized server-side; it can never relax a risk gate.
    scanner_verdict: dict[str, Any] | None = None
    # Bypass the in-memory analyze cache (Feature: analysis caching) and force
    # a fresh LLM call even if a fresh cached proposal exists for this
    # (symbol, tf, htf, resolved provider) key.
    force: bool = False


# --- Trade Reevaluation (advisory review of an ALREADY OPEN position) ---
# Never places orders, moves stops or closes anything — the human applies
# the recommendation via the app's existing SL/close controls.

ReevaluateAction = Literal["HOLD", "MOVE_SL_BE", "PARTIAL_CLOSE", "CLOSE"]


class ReevaluateProposal(BaseModel):
    """Validated LLM reevaluation of an open position. Advisory only."""

    action: ReevaluateAction
    # Reuses the same low/medium/high scale as setup_confidence.
    confidence: SetupConfidence = "medium"
    reason: str = ""
    new_sl: float | None = None
    new_tp: float | None = None
    # Suggested share of the current hold to close (0-100). Only meaningful
    # for PARTIAL_CLOSE; null otherwise.
    partial_close_pct: float | None = Field(None, ge=0, le=100)
    risk_notes: str = ""


class ReevaluateRequest(BaseModel):
    symbol: str = "BTC_USDT"
    tf: str = "15m"
    htf: str = "1H"


# --- Order ticket (user-submitted; gates re-validate) ---

OrderSide = Literal["long", "short"]
OrderType = Literal["market", "limit"]
# auto = SL/TP as exchange trigger orders (protected).
# manual = NO exchange SL/TP; the trader manages the exit themselves.
TriggerMode = Literal["auto", "manual"]


class OrderTicket(BaseModel):
    """Manual / applied order ticket. Never place without preview token + arming."""

    symbol: str
    side: OrderSide
    order_type: OrderType = "market"
    vol: float = Field(..., gt=0, description="Contract volume (not coin amount)")
    leverage: int = Field(5, ge=1)
    price: float | None = Field(None, description="Limit price; required for limit")
    entry: float | None = Field(
        None, description="Risk entry reference (market: usually last_price)"
    )
    stop_loss: float | None = None
    take_profit: float | None = Field(None, description="TP1")
    scale_out: bool = Field(
        False, description="Two-rung TP ladder: split reduce-only TP across tp1/tp2"
    )
    tp2: float | None = Field(None, description="Second take-profit rung (scale-out)")
    tp1_share: float = Field(
        0.5, gt=0.0, lt=1.0, description="Fraction of size exiting at tp1"
    )
    open_type: int = Field(
        1, description="MEXC openType: 1 isolated, 2 cross"
    )
    trigger_mode: TriggerMode = Field(
        "auto",
        description="auto = exchange SL/TP triggers; manual = none, trader exits",
    )
    risk_pct: float | None = Field(
        None,
        description=(
            "Requested risk percent for /api/sizing/suggest; server caps it at "
            "settings.max_risk_pct and falls back to that cap if omitted/invalid."
        ),
    )


class ConfirmRequest(BaseModel):
    token: str


class ClosePositionRequest(BaseModel):
    """Market-close an open position (or part of it). Requires arming."""

    symbol: str
    side: OrderSide
    vol: float | None = Field(None, gt=0, description="None = close full position")
    fraction: float | None = Field(
        None, gt=0, le=1, description="Share of current hold to close (0<f<=1)"
    )


class CancelRequest(BaseModel):
    order_id: str | int | None = None
    orderId: str | int | None = None  # alias for MEXC-style clients
    symbol: str | None = None

    def resolved_order_id(self) -> str | int | None:
        return self.order_id if self.order_id is not None else self.orderId


class ModifySLRequest(BaseModel):
    """Move/replace the stop-loss of an OPEN position. Requires arming."""

    symbol: str
    side: OrderSide
    new_sl: float = Field(..., gt=0, description="New stop-loss price")


class ArmRequest(BaseModel):
    """Arm autonomous management rules for ONE live position (spec §3.1/§4).

    This ENABLES autonomous stop moves, but the endpoint itself never moves a
    stop or places an order — it only writes the mgmt DB record; the server-side
    monitor is the sole actor. `side` is validated to long/short by OrderSide;
    the endpoint additionally whitelists the rule NAMES (v1: only `auto_be`).
    """

    symbol: str
    side: OrderSide
    rules: dict[str, Any] = Field(default_factory=dict)


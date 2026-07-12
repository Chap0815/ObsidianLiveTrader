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
    # Overall confidence in the setup (distinct from pattern_confidence): the
    # prompt requires "low" whenever LTF momentum is turning against the
    # trade direction or the achievable rrr is below risk_policy.min_rrr.
    # Default "medium" so older/loose LLM output that omits the field still
    # validates (see app/llm/client.py parse_proposal normalization).
    setup_confidence: SetupConfidence = "medium"
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
    rationale: str = ""


class AnalyzeRequest(BaseModel):
    symbol: str = "BTC_USDT"
    tf: str = "15m"
    htf: str = "1H"
    # Optional pre-screen from the market scanner for THIS coin (bias/setup/
    # key_level/score). Advisory only — the analyzer confirms or refutes it and
    # it is sanitized server-side; it can never relax a risk gate.
    scanner_verdict: dict[str, Any] | None = None


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


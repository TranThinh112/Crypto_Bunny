from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


Side = Literal["long", "short"]
Mode = Literal["dry_run", "demo", "live"]


@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published_at: datetime
    summary: str
    symbols: list[str]
    sentiment_score: float
    sentiment_label: str


@dataclass
class NewsDigest:
    items: list[NewsItem]
    by_symbol_score: dict[str, float]
    by_symbol_count: dict[str, int]


@dataclass
class MarketSnapshot:
    symbol: str
    timestamp: datetime
    last: float
    bid: float | None
    ask: float | None
    spread_pct: float | None
    ema_fast: float
    ema_slow: float
    rsi: float
    atr: float
    atr_pct: float
    volume_ratio: float
    support: float
    resistance: float
    higher_timeframes: dict[str, dict[str, Any]] = field(default_factory=dict)
    candlestick_patterns: dict[str, Any] = field(default_factory=dict)
    ohlcv_timeframe: str | None = None
    ohlcv: list[list[float]] = field(default_factory=list)
    market_pattern_analysis: dict[str, Any] = field(default_factory=dict)
    ema200: float | None = None
    vwap: float | None = None
    adx: float | None = None
    funding_rate: float | None = None
    open_interest: float | None = None
    open_interest_change: float | None = None
    fear_greed: float | None = None
    news_score: float | None = None


@dataclass
class TradeCandidate:
    symbol: str
    base: str
    side: Side
    confidence: float
    entry: float
    stop_loss: float
    take_profit: float
    risk_reward: float
    order_usdt: float
    quantity: float | None
    spread_pct: float | None
    news_score: float
    news_count: int
    higher_timeframes: dict[str, dict[str, Any]] = field(default_factory=dict)
    indicator_summary: dict[str, Any] = field(default_factory=dict)
    candlestick_patterns: dict[str, Any] = field(default_factory=dict)
    rule_score: float | None = None
    margin_usdt: float | None = None
    recovery_margin_usdt: float | None = None
    recovery_source_key: str | None = None
    sizing_notes: list[str] = field(default_factory=list)
    win_probability_pct: float | None = None
    target_mode: str = "atr_rr"
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    price_take_profit_pct: float | None = None
    price_stop_loss_pct: float | None = None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    previous_win_probability_pct: float | None = None
    win_delta_pct: float | None = None
    scan_source: str = "new_scan"
    setup_quality: str | None = None
    position_slot: int | None = None
    risk_percent: float | None = None
    market_regime: str | None = None
    regime_confidence: float | None = None
    decision_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def planned_risk_usdt(self) -> float:
        stop_pct = abs(self.entry - self.stop_loss) / self.entry
        return self.order_usdt * stop_pct


@dataclass
class RiskCheck:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    mode: str
    submitted: bool
    order_id: str | None
    message: str
    raw: dict[str, Any] | None = None
    journal_type: str | None = None
    journal_id: int | None = None
    linked_journal_id: int | None = None


@dataclass
class Decision:
    created_at: datetime
    mode: str
    action: str
    selected: TradeCandidate | None
    candidates: list[TradeCandidate]
    risk_check: RiskCheck
    execution: ExecutionResult | None
    news_items: list[NewsItem]
    scan_comparison: dict[str, Any] = field(default_factory=dict)


def to_jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    return value

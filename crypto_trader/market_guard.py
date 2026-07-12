from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .market import create_exchange
from .models import to_jsonable
from .storage import (
    get_journal_state,
    list_market_guard_observations,
    prune_market_guard_observations,
    save_market_guard_observation,
    set_journal_state,
)


BLOCK_UNTIL_KEY = "market_guard_block_until"
LATEST_STATUS_KEY = "market_guard_latest_status"


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return (current - previous) / previous * 100


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _max(values: list[float]) -> float:
    return max(values) if values else 0.0


def _guard_config(config: dict[str, Any]) -> dict[str, Any]:
    return config.get("market_guard", {})


def market_guard_enabled(config: dict[str, Any]) -> bool:
    return bool(_guard_config(config).get("enabled", True))


def market_guard_interval(config: dict[str, Any]) -> int:
    return max(30, int(_guard_config(config).get("interval_seconds", 60) or 60))


def market_guard_notify_interval(config: dict[str, Any]) -> int:
    return max(60, int(_guard_config(config).get("notify_interval_seconds", 600) or 600))


def market_guard_block_status(config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    until = _parse_time(get_journal_state(config, BLOCK_UNTIL_KEY))
    active = bool(until and until > now)
    return {
        "active": active,
        "blocked_until": until.isoformat() if until else None,
        "remaining_seconds": round((until - now).total_seconds()) if active and until else 0,
    }


def _set_block_until(config: dict[str, Any], until: datetime) -> None:
    current = _parse_time(get_journal_state(config, BLOCK_UNTIL_KEY))
    if current and current > until:
        return
    set_journal_state(config, BLOCK_UNTIL_KEY, until.isoformat())


def _observed_at_from_row(row: list[float], fallback: datetime) -> str:
    try:
        timestamp = float(row[0])
    except (TypeError, ValueError):
        return fallback.isoformat()
    if timestamp <= 0:
        return fallback.isoformat()
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _candle_observation(
    symbol: str,
    rows: list[list[float]],
    guard_config: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if len(rows) < 8:
        return {
            "created_at": now.isoformat(),
            "observed_at": now.isoformat(),
            "symbol": symbol,
            "severity": "normal",
            "last": None,
            "move_pct": 0.0,
            "candle_range_pct": 0.0,
            "wick_pct": 0.0,
            "wick_body_ratio": 0.0,
            "volume_ratio": 1.0,
            "reasons": [],
        }

    lookback = max(2, int(guard_config.get("lookback_candles", 5) or 5))
    current = rows[-1]
    previous = rows[-lookback - 1] if len(rows) > lookback else rows[0]
    open_price = float(current[1])
    high = float(current[2])
    low = float(current[3])
    close = float(current[4])
    last = close
    previous_close = float(previous[4])
    volume = float(current[5] or 0)
    history_volume = [float(row[5] or 0) for row in rows[-21:-1]]
    average_volume = _avg(history_volume)
    volume_ratio = volume / average_volume if average_volume > 0 else 1.0

    move_pct = _pct_change(last, previous_close)
    candle_range_pct = ((high - low) / last * 100) if last else 0.0
    body = abs(close - open_price)
    upper_wick = max(0.0, high - max(open_price, close))
    lower_wick = max(0.0, min(open_price, close) - low)
    max_wick = max(upper_wick, lower_wick)
    wick_pct = (max_wick / last * 100) if last else 0.0
    wick_body_ratio = max_wick / max(body, last * 0.0002)

    move_threshold = float(guard_config.get("price_move_5m_pct", 0.8) or 0.8)
    critical_move_threshold = float(guard_config.get("critical_price_move_5m_pct", 1.4) or 1.4)
    range_threshold = float(guard_config.get("candle_range_pct", 0.9) or 0.9)
    critical_range_threshold = float(guard_config.get("critical_candle_range_pct", 1.8) or 1.8)
    wick_threshold = float(guard_config.get("wick_pct", 0.45) or 0.45)
    wick_ratio_threshold = float(guard_config.get("wick_body_ratio", 2.5) or 2.5)
    volume_threshold = float(guard_config.get("volume_ratio", 2.5) or 2.5)

    reasons: list[str] = []
    if abs(move_pct) >= move_threshold:
        direction = "tăng" if move_pct > 0 else "giảm"
        reasons.append(f"giá {direction} {move_pct:+.2f}%/{lookback} phút")
    if candle_range_pct >= range_threshold:
        reasons.append(f"biên nến {candle_range_pct:.2f}%")
    if wick_pct >= wick_threshold and wick_body_ratio >= wick_ratio_threshold:
        wick_side = "râu trên" if upper_wick >= lower_wick else "râu dưới"
        reasons.append(f"{wick_side} {wick_pct:.2f}%")
    if volume_ratio >= volume_threshold:
        reasons.append(f"volume {volume_ratio:.2f}x")

    severity = "warning"
    if not reasons:
        severity = "normal"
    elif (
        abs(move_pct) >= critical_move_threshold
        or candle_range_pct >= critical_range_threshold
        or (wick_pct >= wick_threshold and volume_ratio >= volume_threshold)
    ):
        severity = "critical"

    return {
        "created_at": now.isoformat(),
        "observed_at": _observed_at_from_row(current, now),
        "symbol": symbol,
        "severity": severity,
        "last": round(last, 8),
        "move_pct": round(move_pct, 4),
        "candle_range_pct": round(candle_range_pct, 4),
        "wick_pct": round(wick_pct, 4),
        "wick_body_ratio": round(wick_body_ratio, 4),
        "volume_ratio": round(volume_ratio, 4),
        "reasons": reasons,
    }


def _candle_alert(
    symbol: str,
    rows: list[list[float]],
    guard_config: dict[str, Any],
) -> dict[str, Any] | None:
    observation = _candle_observation(symbol, rows, guard_config)
    if observation.get("severity") == "normal":
        return None
    return observation


def _store_latest_status(config: dict[str, Any], status: dict[str, Any]) -> None:
    set_journal_state(config, LATEST_STATUS_KEY, json.dumps(to_jsonable(status), ensure_ascii=False))


def latest_market_guard_status(config: dict[str, Any]) -> dict[str, Any] | None:
    raw = get_journal_state(config, LATEST_STATUS_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _numbers(rows: list[dict[str, Any]], key: str, *, absolute: bool = False) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = _float(row.get(key))
        if value is None:
            continue
        values.append(abs(value) if absolute else value)
    return values


def _window_summary(rows: list[dict[str, Any]], window_minutes: int) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda item: str(item.get("observed_at") or ""))
    latest = ordered[-1] if ordered else {}
    first = ordered[0] if ordered else {}
    first_price = _float(first.get("last"))
    latest_price = _float(latest.get("last"))
    window_move = _pct_change(latest_price, first_price) if latest_price is not None and first_price else 0.0
    alerts = [row for row in ordered if str(row.get("severity")) in {"warning", "critical"}]
    critical = [row for row in ordered if str(row.get("severity")) == "critical"]
    max_abs_move = _max(_numbers(ordered, "move_pct", absolute=True))
    max_range = _max(_numbers(ordered, "candle_range_pct"))
    max_wick = _max(_numbers(ordered, "wick_pct"))
    max_volume = _max(_numbers(ordered, "volume_ratio"))
    avg_volume = _avg(_numbers(ordered, "volume_ratio"))
    risk_score = min(
        10.0,
        len(alerts) * 1.2
        + len(critical) * 2.5
        + max(0.0, max_volume - 1.0) * 0.8
        + max_abs_move * 1.1
        + max_wick * 1.3,
    )
    if window_move >= 0.35:
        direction = "up"
    elif window_move <= -0.35:
        direction = "down"
    else:
        direction = "neutral"
    if critical or risk_score >= 6:
        action = "avoid_new_entry"
    elif alerts or risk_score >= 3.5:
        action = "wait_confirmation"
    else:
        action = "normal"
    return {
        "window_minutes": window_minutes,
        "sample_count": len(ordered),
        "alert_count": len(alerts),
        "critical_count": len(critical),
        "latest_price": latest_price,
        "window_move_pct": round(window_move, 4),
        "max_abs_move_pct": round(max_abs_move, 4),
        "max_candle_range_pct": round(max_range, 4),
        "max_wick_pct": round(max_wick, 4),
        "max_volume_ratio": round(max_volume, 4),
        "avg_volume_ratio": round(avg_volume, 4),
        "risk_score": round(risk_score, 4),
        "direction": direction,
        "action": action,
        "latest_observed_at": latest.get("observed_at"),
    }


def market_guard_symbol_layers(config: dict[str, Any], symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    guard_config = _guard_config(config)
    five_samples = max(1, int(guard_config.get("layer_5m_samples", 5) or 5))
    twenty_samples = max(five_samples, int(guard_config.get("layer_20m_samples", 20) or 20))
    symbols = symbols or list(dict.fromkeys(config.get("strategy", {}).get("symbols", [])))
    layers: dict[str, dict[str, Any]] = {}
    for symbol in symbols:
        rows = list_market_guard_observations(config, symbol=symbol, limit=twenty_samples)
        ordered_desc = rows
        recent_5 = list(reversed(ordered_desc[:five_samples]))
        recent_20 = list(reversed(ordered_desc[:twenty_samples]))
        layers[symbol] = {
            "symbol": symbol,
            "layer_1m": ordered_desc[0] if ordered_desc else None,
            "layer_5m": _window_summary(recent_5, 5),
            "layer_20m": _window_summary(recent_20, 20),
        }
    return layers


def market_guard_top_risk(layers: dict[str, dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, layer in layers.items():
        layer_5m = layer.get("layer_5m") or {}
        layer_20m = layer.get("layer_20m") or {}
        score = max(float(layer_5m.get("risk_score") or 0), float(layer_20m.get("risk_score") or 0))
        if score <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "risk_score": round(score, 4),
                "action_5m": layer_5m.get("action"),
                "action_20m": layer_20m.get("action"),
                "move_5m_pct": layer_5m.get("window_move_pct"),
                "move_20m_pct": layer_20m.get("window_move_pct"),
                "max_volume_5m": layer_5m.get("max_volume_ratio"),
                "max_volume_20m": layer_20m.get("max_volume_ratio"),
            }
        )
    rows.sort(key=lambda item: float(item.get("risk_score") or 0), reverse=True)
    return rows[:limit]


def run_market_guard(config: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    guard_config = _guard_config(config)
    if not market_guard_enabled(config):
        status = {
            "enabled": False,
            "created_at": now.isoformat(),
            "alerts": [],
            "warnings": [],
            "block": market_guard_block_status(config, now),
        }
        _store_latest_status(config, status)
        return status

    timeframe = str(guard_config.get("timeframe", "1m") or "1m")
    limit = max(30, int(guard_config.get("ohlcv_limit", 40) or 40))
    symbols = list(dict.fromkeys(config.get("strategy", {}).get("symbols", [])))
    max_symbols = int(guard_config.get("max_symbols", 20) or 20)
    symbols = symbols[:max_symbols]
    alerts: list[dict[str, Any]] = []
    warnings: list[str] = []

    try:
        exchange = create_exchange(config, authenticated=False)
        exchange.load_markets()
    except Exception as exc:
        status = {
            "enabled": True,
            "created_at": now.isoformat(),
            "alerts": [],
            "warnings": [f"Market guard không lấy được dữ liệu sàn: {exc}"],
            "block": market_guard_block_status(config, now),
        }
        _store_latest_status(config, status)
        return status

    for symbol in symbols:
        try:
            rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            observation = _candle_observation(symbol, rows, guard_config)
            save_market_guard_observation(config, observation)
            if observation.get("severity") != "normal":
                alerts.append(observation)
        except Exception as exc:
            warnings.append(f"{symbol}: guard fetch failed: {exc}")

    keep_hours = max(1, int(guard_config.get("memory_keep_hours", 24) or 24))
    prune_market_guard_observations(config, keep_hours=keep_hours)
    layers = market_guard_symbol_layers(config, symbols)
    severity_rank = {"critical": 2, "warning": 1}
    alerts.sort(key=lambda item: (severity_rank.get(str(item.get("severity")), 0), abs(float(item.get("move_pct") or 0))), reverse=True)
    if any(alert.get("severity") == "critical" for alert in alerts):
        pause_seconds = max(60, int(guard_config.get("pause_new_entries_seconds", 900) or 900))
        _set_block_until(config, now + timedelta(seconds=pause_seconds))

    status = {
        "enabled": True,
        "created_at": now.isoformat(),
        "timeframe": timeframe,
        "interval_seconds": market_guard_interval(config),
        "notify_interval_seconds": market_guard_notify_interval(config),
        "alerts": alerts,
        "layers": {
            "top_risk": market_guard_top_risk(layers, limit=5),
            "symbols": layers,
        },
        "warnings": warnings,
        "block": market_guard_block_status(config, now),
    }
    _store_latest_status(config, status)
    return status

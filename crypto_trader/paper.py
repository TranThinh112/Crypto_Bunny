from __future__ import annotations

from typing import Any

from .models import Decision
from .storage import active_paper_trades, close_paper_trade, list_paper_trades, open_paper_trade


def _candidate_prices(decision: Decision) -> dict[str, float]:
    return {candidate.symbol: candidate.entry for candidate in decision.candidates}


def _close_hit_trades(config: dict[str, Any], decision: Decision) -> list[dict[str, Any]]:
    prices = _candidate_prices(decision)
    closed: list[dict[str, Any]] = []
    for trade in active_paper_trades(config):
        symbol = str(trade["symbol"])
        if symbol not in prices:
            continue
        current = float(prices[symbol])
        side = str(trade["side"])
        take_profit = float(trade["take_profit"])
        stop_loss = float(trade["stop_loss"])
        if side == "long" and current >= take_profit:
            closed.append(close_paper_trade(config, int(trade["id"]), current, "TP"))
        elif side == "long" and current <= stop_loss:
            closed.append(close_paper_trade(config, int(trade["id"]), current, "SL"))
        elif side == "short" and current <= take_profit:
            closed.append(close_paper_trade(config, int(trade["id"]), current, "TP"))
        elif side == "short" and current >= stop_loss:
            closed.append(close_paper_trade(config, int(trade["id"]), current, "SL"))
    return closed


def simulate_paper_scan(config: dict[str, Any], decision: Decision) -> dict[str, Any]:
    paper_config = config.get("paper_trading", {})
    if not paper_config.get("enabled", True):
        return {
            "enabled": False,
            "opened": None,
            "closed": [],
            "message": "Paper trading is disabled",
            "trades": list_paper_trades(config, limit=20),
        }

    closed = _close_hit_trades(config, decision)
    active = active_paper_trades(config)
    selected = decision.selected
    opened = None
    message = "Không có lệnh mô phỏng mới"

    if selected and decision.risk_check.passed:
        max_active = int(paper_config.get("max_active_trades", 1))
        duplicate = any(
            trade["symbol"] == selected.symbol and trade["side"] == selected.side for trade in active
        )
        if duplicate:
            message = "Đã có lệnh mô phỏng cùng cặp và cùng hướng"
        elif len(active) >= max_active:
            message = f"Đang có {len(active)}/{max_active} lệnh mô phỏng mở"
        else:
            opened = open_paper_trade(config, selected)
            message = f"Đã mở lệnh mô phỏng {selected.symbol} {selected.side.upper()}"
    elif decision.risk_check.reasons:
        message = decision.risk_check.reasons[0]

    return {
        "enabled": True,
        "opened": opened,
        "closed": closed,
        "message": message,
        "trades": list_paper_trades(config, limit=20),
    }

from __future__ import annotations


def ema(values: list[float], period: int) -> float:
    if not values:
        raise ValueError("ema requires at least one value")
    period = max(1, min(period, len(values)))
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = (value - current) * multiplier + current
    return current


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) <= period:
        return 50.0
    gains: list[float] = []
    losses: list[float] = []
    for previous, current in zip(values[-period - 1 : -1], values[-period:]):
        change = current - previous
        gains.append(max(change, 0.0))
        losses.append(abs(min(change, 0.0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(ohlcv: list[list[float]], period: int = 14) -> float:
    if len(ohlcv) < 2:
        return 0.0
    true_ranges: list[float] = []
    recent = ohlcv[-period - 1 :]
    for previous, current in zip(recent[:-1], recent[1:]):
        previous_close = previous[4]
        high = current[2]
        low = current[3]
        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )
    if not true_ranges:
        return 0.0
    return sum(true_ranges) / len(true_ranges)


def adx(ohlcv: list[list[float]], period: int = 14) -> float:
    if len(ohlcv) <= period * 2:
        return 0.0
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    true_ranges: list[float] = []
    for previous, current in zip(ohlcv[:-1], ohlcv[1:]):
        previous_high = float(previous[2])
        previous_low = float(previous[3])
        previous_close = float(previous[4])
        high = float(current[2])
        low = float(current[3])
        up_move = high - previous_high
        down_move = previous_low - low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    dx_values: list[float] = []
    for index in range(period, len(true_ranges) + 1):
        tr_sum = sum(true_ranges[index - period : index])
        if tr_sum <= 0:
            continue
        plus_di = 100.0 * sum(plus_dm[index - period : index]) / tr_sum
        minus_di = 100.0 * sum(minus_dm[index - period : index]) / tr_sum
        denominator = plus_di + minus_di
        if denominator <= 0:
            continue
        dx_values.append(100.0 * abs(plus_di - minus_di) / denominator)
    if not dx_values:
        return 0.0
    return sum(dx_values[-period:]) / min(period, len(dx_values))


def volume_ratio(ohlcv: list[list[float]], period: int = 20) -> float:
    if len(ohlcv) < 2:
        return 1.0
    recent_volume = ohlcv[-1][5]
    history = [row[5] for row in ohlcv[-period - 1 : -1]]
    if not history:
        return 1.0
    average = sum(history) / len(history)
    if average == 0:
        return 1.0
    return recent_volume / average


def vwap(ohlcv: list[list[float]], period: int | None = None) -> float | None:
    rows = ohlcv[-period:] if period and period > 0 else ohlcv
    total_volume = 0.0
    weighted_price = 0.0
    for row in rows:
        if len(row) < 6:
            continue
        high = float(row[2])
        low = float(row[3])
        close = float(row[4])
        volume = float(row[5])
        if volume <= 0:
            continue
        typical_price = (high + low + close) / 3.0
        weighted_price += typical_price * volume
        total_volume += volume
    if total_volume <= 0:
        return None
    return weighted_price / total_volume

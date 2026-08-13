"""Pure, deterministic indicators over validated completed RTH bars."""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

from .models import CompletedBar15m, require_int, require_number


def validate_bar_series(bars: Sequence[CompletedBar15m]) -> Tuple[CompletedBar15m, ...]:
    """Validate ordering, identity and within-session completeness.

    An overnight transition remains one continuous price series: the prior
    RTH close is intentionally the previous close for the next RTH opening
    bar.  Extended-hours bars themselves cannot enter the model.
    """

    if isinstance(bars, (str, bytes)) or not isinstance(bars, Sequence):
        raise TypeError("bars must be a sequence")
    result = tuple(bars)
    if not result:
        raise ValueError("bars must not be empty")
    symbol = result[0].symbol if type(result[0]) is CompletedBar15m else None
    previous = None
    for index, bar in enumerate(result):
        if type(bar) is not CompletedBar15m:
            raise TypeError("bars[%d] must be an exact CompletedBar15m" % index)
        if not bar.complete:
            raise ValueError("incomplete bars are forbidden")
        if bar.symbol != symbol:
            raise ValueError("all bars must have the same symbol")
        # CompletedBar15m already guarantees finite, coherent OHLCV data.  Do
        # not silently support mutated/forged instances should callers bypass
        # its constructor.
        for field_name in ("open", "high", "low", "close", "volume"):
            if not math.isfinite(getattr(bar, field_name)):
                raise ValueError("bars[%d].%s must be finite" % (index, field_name))
        if previous is not None:
            if bar.end <= previous.end:
                raise ValueError("bars must be strictly chronological and unique")
            if bar.session_date == previous.session_date and bar.start != previous.end:
                raise ValueError("missing or overlapping intraday bar")
            if bar.session_date < previous.session_date:
                raise ValueError("session dates must be chronological")
        previous = bar
    return result


def _period(value: object) -> int:
    period = require_int("period", value)
    if period < 2:
        raise ValueError("period must be at least 2")
    return period


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def wilder_rsi(
    bars: Sequence[CompletedBar15m], period: int = 14
) -> Tuple[Optional[float], ...]:
    """Compute canonical Wilder RSI, aligned with ``bars``.

    The first RSI uses the arithmetic mean of the first ``period`` close
    changes and therefore appears at index ``period``.  The series does not
    reset at an RTH date boundary; the overnight close-to-close change is
    included, while non-RTH bars remain forbidden by the bar model.
    """

    checked = validate_bar_series(bars)
    p = _period(period)
    output = [None] * len(checked)  # type: list[Optional[float]]
    if len(checked) <= p:
        return tuple(output)

    deltas = [checked[i].close - checked[i - 1].close for i in range(1, len(checked))]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    avg_gain = math.fsum(gains[:p]) / p
    avg_loss = math.fsum(losses[:p]) / p
    output[p] = _rsi_value(avg_gain, avg_loss)

    for delta_index in range(p, len(deltas)):
        avg_gain = ((p - 1) * avg_gain + gains[delta_index]) / p
        avg_loss = ((p - 1) * avg_loss + losses[delta_index]) / p
        output[delta_index + 1] = _rsi_value(avg_gain, avg_loss)
    return tuple(output)

def wilder_atr(
    bars: Sequence[CompletedBar15m],
    period: int = 14,
    prior_regular_close: Optional[float] = None,
) -> Tuple[Optional[float], ...]:
    """Compute Wilder ATR with explicit support for a preceding RTH close.

    If ``prior_regular_close`` is omitted, the first true range is simply the
    first bar's high-low.  Every subsequent bar, including the first bar of a
    later RTH session, uses the immediately preceding RTH bar close.
    """

    checked = validate_bar_series(bars)
    p = _period(period)
    if prior_regular_close is not None:
        previous_close = require_number(
            "prior_regular_close", prior_regular_close, positive=True
        )
    else:
        previous_close = None

    true_ranges = []
    for bar in checked:
        if previous_close is None:
            true_range = bar.high - bar.low
        else:
            true_range = max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        true_ranges.append(true_range)
        previous_close = bar.close

    output = [None] * len(checked)  # type: list[Optional[float]]
    if len(checked) < p:
        return tuple(output)
    atr = math.fsum(true_ranges[:p]) / p
    output[p - 1] = atr
    for index in range(p, len(checked)):
        atr = ((p - 1) * atr + true_ranges[index]) / p
        output[index] = atr
    return tuple(output)


def simple_moving_average(
    values: Sequence[float], period: int
) -> Tuple[Optional[float], ...]:
    """Small validated SMA helper used by daily-trend providers."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("values must be a sequence")
    p = _period(period)
    normalized = tuple(
        require_number("values[%d]" % index, value)
        for index, value in enumerate(values)
    )
    output = [None] * len(normalized)  # type: list[Optional[float]]
    if len(normalized) < p:
        return tuple(output)
    running = math.fsum(normalized[:p])
    output[p - 1] = running / p
    for index in range(p, len(normalized)):
        running += normalized[index] - normalized[index - p]
        output[index] = running / p
    return tuple(output)

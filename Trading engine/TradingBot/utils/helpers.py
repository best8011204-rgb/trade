from __future__ import annotations

from decimal import Decimal, ROUND_DOWN


def quantize_down(value: float, step: str) -> float:
    """Round a numeric value down to the exchange-supported increment."""

    decimal_step = Decimal(step)
    decimal_value = Decimal(str(value))
    return float(decimal_value.quantize(decimal_step, rounding=ROUND_DOWN))


"""The number formats and units the run shares, one copy each."""

import math

# A figure worked out in floats can land a hair below a half, as 730 x 0.35 gives 255.49999999999997 for 255.5; this
# tiny addition carries it past that noise before rounding.
CARRY = 1e-9
GIB = 2**30
MIB_PER_GIB = 1024
SECONDS_PER_MINUTE = 60


def half_up(value: float) -> int:
    """Round to the nearest whole number, halves up (round() sends halves to the even number), past float noise."""
    return math.floor(value + 0.5 + CARRY)


def usd(dollars: int) -> str:
    """Write whole US dollars with thousands separators, such as $1,234."""
    return f"${dollars:,}"


def count(n: int, noun: str) -> str:
    """Write a number and its noun, plural unless the number is 1, such as 1 machine or 1,200 machines."""
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def cents(dollars: float) -> float:
    """Round US dollars to whole cents, halves up, past float noise."""
    return round(dollars + CARRY, 2)

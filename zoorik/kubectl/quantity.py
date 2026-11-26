"""Kubernetes quantities, read exactly and rounded up the way the scheduler counts them."""

import math
import re
from fractions import Fraction
from typing import Dict, Mapping

from zoorik.errors import UnexpectedResponse

# A signed decimal number, then a binary suffix, a decimal suffix or a decimal exponent, as the API writes them.
QUANTITY = re.compile(
    r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))"
    r"(?:(Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)|[eE]([+-]?[0-9]+))?"
)
SUFFIXES: Mapping[str, Fraction] = {
    "n": Fraction(1, 10**9),
    "u": Fraction(1, 10**6),
    "m": Fraction(1, 10**3),
    "k": Fraction(10**3),
    "M": Fraction(10**6),
    "G": Fraction(10**9),
    "T": Fraction(10**12),
    "P": Fraction(10**15),
    "E": Fraction(10**18),
    "Ki": Fraction(2**10),
    "Mi": Fraction(2**20),
    "Gi": Fraction(2**30),
    "Ti": Fraction(2**40),
    "Pi": Fraction(2**50),
    "Ei": Fraction(2**60),
}
CPU = "cpu"
MILLI = 1000


def quantity(text: str) -> Fraction:
    """Read a quantity, such as 250m, 1.5Gi or 1e3, as an exact number; any other text is unexpected."""
    match = QUANTITY.fullmatch(text)
    if match is None:
        raise UnexpectedResponse(f"unexpected answer: {text!r} is not a Kubernetes quantity")
    number, suffix, exponent = match.groups()
    if suffix is not None:
        return Fraction(number) * SUFFIXES[suffix]
    return Fraction(number) * Fraction(10) ** (0 if exponent is None else int(exponent))


def scheduler_units(resource: str, amount: Fraction) -> int:
    """Count an amount as the scheduler does: CPU in millicores, anything else in whole units, each rounded up."""
    return math.ceil(amount * MILLI) if resource == CPU else math.ceil(amount)


def resources(values: Mapping[str, str]) -> Dict[str, int]:
    """Read a resource list, such as a container's requests, in scheduler units."""
    return {resource: scheduler_units(resource, quantity(text)) for resource, text in values.items()}

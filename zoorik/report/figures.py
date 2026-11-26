"""How the report writes its figures: savings, shares, sizes, cores, memory and disk space, one rule each."""

import re

from zoorik.cloud.keys import AWS
from zoorik.format import GIB, half_up, usd
from zoorik.report.blocks import Cell
from zoorik.report.pdf import GREEN_INK, SEPARATOR

MINUS = "\u2212"
ARROW = "\u2192"
TIMES = "\xd7"
# The middle dot between the items of a meta line, such as a cluster's platform and its region.
DOT = f" {SEPARATOR} "
# GB in a TB, as disk sizes count them.
TB = 1000
# The smallest figure that two decimals write as more than 0.
SMALLEST = 0.005
# A reason that opens with a plain word takes a capital; one that opens with a size, such as m4.xlarge, doesn't.
PLAIN_WORD = re.compile(r"([a-z']+) ")
# The programs a reason may open with, which keep their small first letter.
COMMANDS = frozenset({"az", "aws", "kubectl", "kubelogin"})


def less(dollars: int) -> str:
    """Write a saving: the minus sign and the dollars, or $0."""
    return MINUS + usd(dollars) if dollars > 0 else "$0"


def percent(part: int, whole: int) -> int:
    """Give part's share of whole in whole percent, halves up; a share of nothing is 0."""
    return half_up(100 * part / whole) if whole > 0 else 0


def pill(saving: int, today: int) -> str:
    """Write a card's saving a month and its share of the cost today, or $0 a month."""
    return f"{less(saving)} a month{DOT}{percent(saving, today)}%" if saving > 0 else "$0 a month"


def saving_cell(dollars: int) -> Cell:
    """Give a saving's cell: bold and green when there is one, else $0."""
    return Cell(less(dollars), bold=True, color=GREEN_INK) if dollars > 0 else Cell("$0")


def change_cell(today: int, after: int) -> Cell:
    """Give a cost's change: a saving as saving_cell writes it, or a rise with its plus sign."""
    return Cell(f"+{usd(after - today)}") if after > today else saving_cell(today - after)


def size_name(cloud: str, size: str) -> str:
    """Write a machine or VM size as people say it: Azure's Standard_E8s_v5 is E8s v5; AWS types stay as they are."""
    return size if cloud == AWS else re.sub(r"^Standard_", "", size).replace("_", " ")


def amount(value: float) -> str:
    """Write cores or GiB with up to two decimals; a little above nothing is <0.01."""
    if value == 0:
        return "0"
    # Two decimals would write a small figure as 0, which would read as nothing.
    if value < SMALLEST:
        return "<0.01"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def cores(millicores: int) -> str:
    """Write CPU in cores."""
    return amount(millicores / 1000)


def gib(memory: int) -> str:
    """Write memory in GiB."""
    return amount(memory / GIB)


def space(gib_used: float) -> str:
    """Write disk space as the disk pages always have: GB below 1,000, then TB with one decimal."""
    rounded = half_up(gib_used)
    return f"{rounded / TB:.1f} TB" if rounded >= TB else f"{rounded} GB"


def sentence(text: str) -> str:
    """Give a reason as a sentence: a capital on its first word, unless that is a command or a size."""
    word = PLAIN_WORD.match(text)
    if word is None or word.group(1) in COMMANDS:
        return text
    return text[:1].upper() + text[1:]

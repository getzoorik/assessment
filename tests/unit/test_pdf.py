"""The PDF engine and the report's blocks: a made-up report that uses every block, and the rules they follow."""

import unittest
from dataclasses import dataclass
from typing import List, Sequence, Union

from tests.replay.golden import check, check_structure, pdf_facts
from zoorik.report.blocks import (
    BANNER,
    BOTTOM,
    CARD_GAP,
    GAP,
    SECTION_GAP,
    TOP,
    Card,
    Cell,
    Column,
    Key,
    Kpi,
    Kpis,
    Legend,
    Name,
    NextSteps,
    Report,
    Section,
    Table,
    Title,
    fine,
    headline,
    text,
)
from zoorik.report.pdf import (
    BLUE,
    DEEP,
    GREEN_INK,
    MINT,
    MUTED,
    RULE,
    WHITE,
    Box,
    Line,
    Pdf,
    Span,
    Style,
    text_width,
    to_winansi,
    wrap,
)

DATE = "5 January 2026"
PLAIN = Style(10)
HEADLINE: List[Union[str, Span]] = [
    "Reduce your cloud cost by ",
    Span("38%", True, GREEN_INK),
    ", as worked out at ",
    Span("example.com/method", url="https://example.com/method?part=(1)"),
]
KPIS = [
    Kpi("\u2212$4,740", "saved every month,\nabout $56,880 a year", GREEN_INK),
    Kpi("41 \u2192 26", "machines today\nand after"),
    Kpi("38%", "of the cloud cost today"),
]


@dataclass(frozen=True)
class Bar:
    """Art for the tests: a bar whose length is its share of the cell's width."""

    share: float

    def height(self, width: float) -> float:
        """Give the bar's height, which is the same at any width."""
        return 7.5

    def draw(self, pdf: Pdf, x: float, y: float, width: float) -> None:
        """Draw the bar from its top-left corner (x, y), its share of width long."""
        pdf.rect(Box(x, y, width * self.share, 7.5), BLUE, radius=3.75)


# A title card: a bold name over a muted line, art, figures in green, a cell left empty, and a total row.
CLUSTERS = Table(
    [Column(0.3), Column(0.16), Column(0.24), Column(0.1, "r"), Column(0.1, "r"), Column(0.1, "r")],
    ["Cluster", "Machines\ntoday \u2192 after", "", "Today", "After", "Saving"],
    [
        [
            Cell("alpha", "AKS \xb7 region-a \xb7 12 machines", True),
            "12 \u2192 7",
            Cell(art=Bar(0.57)),
            "$4,210",
            "$2,380",
            Cell("\u2212$1,830", bold=True, color=GREEN_INK),
        ],
        [
            Cell("beta-cluster-with-a-hyphenated-name", "EKS \xb7 region-b", True),
            "21 \u2192 14",
            Cell(art=Bar(0.67)),
            "$6,020",
            "$4,010",
            Cell("\u2212$2,010", bold=True, color=GREEN_INK),
        ],
        [
            Cell("gamma", "AKS \xb7 region-c \xb7 no list price", True),
            "8 \u2192 5",
            Cell(art=Bar(0.33)),
            Cell("not priced", color=MUTED),
            "",
            "",
        ],
    ],
    ["All 3 clusters", "41 \u2192 26", "", "$12,480", "$7,740", Cell("\u2212$4,740", color=GREEN_INK)],
)
POOLS = Table(
    [Column(0.3), Column(0.45), Column(0.25, "r")],
    ["Node pool", "Machines", "Saving"],
    [
        [Cell("system", "4 vCPU, 16 GiB", True), "3 \u2192 2", Cell("\u2212$140", bold=True, color=GREEN_INK)],
        [Cell("apps", "8 vCPU, 32 GiB \xb7 spot", True), "9 \u2192 5", "$0"],
    ],
)
# A name and a meta line too long for one line beside the pill, so each wraps.
LONG_NAME = "a-cluster-name-long-enough-to-wrap-beside-its-pill-and-go-on-to-a-second-line"
LONG_META = (
    "EKS \xb7 region-b \xb7 a meta line long enough to wrap \xb7 with separator dots \xb7 at the ends of its lines"
)
# A word wider than its column, letters the fonts lack, characters PDF strings escape, and a total that isn't bold.
WORKLOADS = Table(
    [Column(0.4), Column(0.3, "r"), Column(0.3, "r")],
    ["Workload", "CPU\nrequest \u2192 recommended", "Peak use"],
    [
        [
            Cell("averyveryverylongworkloadnamewithoutanyhyphensthatbreaksbetweenitsletters", "namespace-a", True),
            "2 \u2192 0.5",
            "0.31",
        ],
        [
            Cell(
                "Caf\xe9 fa\xe7ade \u2013 na\xefve \u20ac5 \xd7 3 \u2713 \u0101\tend (a\\b)", "letters the fonts lack"
            ),
            "1 \u2192 1",
            Cell("not read", color=MUTED),
        ],
    ],
    ["All 2 workloads", Cell("3 \u2192 1.5", bold=False), "\u2212"],
)
# Rows enough to split the card over two pages.
EVERY_WORKLOAD = Table(
    [Column(0.5), Column(0.25, "r"), Column(0.25, "r")],
    ["Workload", "Pods\ntoday \u2192 after", "Cost"],
    [
        [
            Cell(f"workload-{number:02d}", f"namespace-{number % 4}"),
            f"{number} \u2192 {number // 2}",
            f"${number * 37:,}",
        ]
        for number in range(1, 41)
    ],
    ["All 40 workloads", "820 \u2192 400", "$30,340"],
)
NEXT_STEPS: List[Union[str, Span]] = [
    "Run the assessment again after a change: ",
    Span("example.com/assess", True, WHITE, "https://example.com/assess"),
    ". Questions go to ",
    Span("hello@example.com", True, WHITE, "mailto:hello@example.com"),
    ".",
]
FINE = " \xb7 ".join(["At public list prices", "an estimate, not a quote", "made-up figures for tests"] * 4)


def sample_report() -> Report:
    """Build a made-up report that uses every block, every kind of card and every rule of wrapping."""
    report = Report("Cloud cost assessment", "Example estate \xb7 made-up figures", DATE, "Example footer \xb7 a test")
    report.add(headline(HEADLINE))
    report.add(Kpis(KPIS), 17)
    report.add(Section(1, "Savings by cluster"), GAP, keep=True)
    clusters_legend = [Key("Today", RULE), Key("After", BLUE)]
    report.add(Card.of(CLUSTERS, Title("Savings by cluster", "a month, at list prices"), clusters_legend), SECTION_GAP)
    report.add(Section(2, "Savings by node pool"), GAP, keep=True)
    machines = [Key("Machine kept", RULE, 6.75, 6.75), Key("Machine to remove", MINT, 6.75, 6.75, True)]
    report.add(Legend(machines), SECTION_GAP, keep=True)
    report.add(
        Card.of(POOLS, Name("alpha", "AKS \xb7 region-a \xb7 12 machines", "\u2212$1,830 \xb7 43%")), SECTION_GAP
    )
    report.add(Card.of(POOLS, Name(LONG_NAME, LONG_META, "\u2212$2,010 a month \xb7 33%")), CARD_GAP)
    report.add(Section(3, "Recommended sizes"), GAP, keep=True)
    sizes = [Key("Reserved today", RULE), Key("Recommended", BLUE), Key("Peak use", DEEP, 2.25, 5.25)]
    report.add(Card.of(WORKLOADS, legend=sizes), SECTION_GAP)
    notes = "Usage not read for one workload: its metrics were not served.\nA second note, on a line of its own."
    report.add(text(notes, Style(8.25, MUTED)), SECTION_GAP)
    report.add(Section(4, "Every workload"), GAP, keep=True)
    report.add(Card.of(EVERY_WORKLOAD), SECTION_GAP)
    report.add(NextSteps("Next steps", NEXT_STEPS), GAP)
    report.add(fine(FINE), SECTION_GAP)
    return report


def texts(lines: Sequence[Line]) -> List[List[str]]:
    """Give the text of each wrapped line's pieces."""
    return [[piece.text for piece in line.pieces] for line in lines]


def room_for(text: str) -> float:
    """Give the width of a line that holds text in PLAIN with a point to spare, clear of rounding at its end."""
    return text_width(text, PLAIN) + 1


def rows(count: int, total: bool) -> Table:
    """Give a table of one column, with count rows and a total row when total is set."""
    return Table([Column(1.0)], ["Row"], [[f"row {number}"] for number in range(count)], ["total"] if total else None)


class Filler:
    """A block for tests: its height, and the y of its top each time it is drawn."""

    def __init__(self, height: float) -> None:
        """Make a block height points tall."""
        self._height = height
        self.drawn: List[float] = []

    @property
    def height(self) -> float:
        """Give the block's height."""
        return self._height

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Note the y of the block's top."""
        self.drawn.append(y)


class SampleTest(unittest.TestCase):
    """A made-up report that uses every block."""

    def test_the_sample_is_a_whole_pdf_that_matches_its_golden(self) -> None:
        """The sample passes the structure check and gives its golden: its pages, its text and its normalized hash."""
        pdf = sample_report().pdf()
        check_structure(pdf)
        check("pdf_sample", pdf_facts(pdf))

    def test_a_link_is_a_uri_annotation_with_its_brackets_escaped(self) -> None:
        """A span with a url becomes a link annotation, its brackets escaped as PDF strings need."""
        self.assertIn(b"/URI (https://example.com/method?part=\\(1\\))", sample_report().pdf())


class WinAnsiTest(unittest.TestCase):
    """Text becomes the code points the standard fonts draw, and is measured with their widths."""

    def test_each_character_becomes_what_the_fonts_draw(self) -> None:
        """WinAnsi characters keep their code, others become their plain letter or ?, and controls become spaces."""
        self.assertEqual(to_winansi("Caf\xe9 \u20ac5 \u2013"), "Caf\xe9 \x805 \x96")
        self.assertEqual(to_winansi("\u0101 \u2713"), "a ?")
        self.assertEqual(to_winansi("a\tb\x7f"), "a b ")
        self.assertEqual(to_winansi("\u2192 \u2212"), "\u2192 \u2212")

    def test_a_width_is_the_sum_of_the_letters_widths(self) -> None:
        """A width adds the letters' widths at the size, and the spacing after each letter."""
        self.assertAlmostEqual(text_width("Zoorik", PLAIN), 27.78)
        self.assertAlmostEqual(text_width("Zoorik", Style(10, bold=True)), 30.56)
        self.assertAlmostEqual(text_width("Zoorik", Style(10, spacing=0.5)), 30.78)
        self.assertAlmostEqual(text_width("\u2212", PLAIN), 5.84)


class WrapTest(unittest.TestCase):
    """Lines break at spaces and newlines, after a hyphen, and between letters only inside a part wider than a line."""

    def test_lines_break_at_spaces_and_newlines(self) -> None:
        """A word that doesn't fit starts the next line, and a newline always does."""
        lines = wrap("one two three\nfour", PLAIN, room_for("one two"))
        self.assertEqual(texts(lines), [["one", "two"], ["three"], ["four"]])

    def test_a_hyphenated_word_that_fits_a_line_moves_whole(self) -> None:
        """A hyphenated word that fits a line goes to the next line whole."""
        self.assertEqual(texts(wrap("a long-word", PLAIN, room_for("long-word"))), [["a"], ["long-", "word"]])

    def test_a_hyphenated_word_wider_than_a_line_breaks_after_a_hyphen(self) -> None:
        """A hyphenated word wider than a line breaks after the last hyphen that fits."""
        lines = wrap("very-long-word", PLAIN, room_for("very-long-"))
        self.assertEqual(texts(lines), [["very-", "long-"], ["word"]])

    def test_a_part_wider_than_a_line_breaks_between_letters(self) -> None:
        """A part wider than a line fills each line letter by letter."""
        self.assertEqual(texts(wrap("abcdefghij", PLAIN, room_for("abcde"))), [["abcde"], ["fghij"]])

    def test_a_separator_dot_at_either_end_of_a_line_is_left_out(self) -> None:
        """A dot left at the end or the start of a line is left out, and the line's width and start follow."""
        at_end = wrap("one \xb7 two", PLAIN, room_for("one \xb7"))
        self.assertEqual(texts(at_end), [["one"], ["two"]])
        self.assertAlmostEqual(at_end[0].width, text_width("one", PLAIN))
        at_start = wrap("one \xb7 two", PLAIN, room_for("\xb7 two"))
        self.assertEqual(texts(at_start), [["one"], ["two"]])
        self.assertEqual(at_start[1].pieces[0].x, 0)
        self.assertAlmostEqual(at_start[1].width, text_width("two", PLAIN))

    def test_a_span_keeps_its_weight_colour_and_link(self) -> None:
        """A span's pieces carry its weight, its colour and its link."""
        [line] = wrap(["see ", Span("here", True, WHITE, "https://example.com")], PLAIN, 100)
        here = line.pieces[1]
        expected = ("here", True, WHITE, "https://example.com")
        self.assertEqual((here.text, here.style.bold, here.style.color, here.url), expected)


class CardTest(unittest.TestCase):
    """A card taller than the room left splits between its rows."""

    def test_a_card_splits_after_the_rows_that_fit(self) -> None:
        """The first part takes the rows that fit and keeps the card's opening; the rest goes on."""
        card = Card.of(rows(4, total=False))
        parts = card.split(card.height - card.rows[-1].height)
        assert parts is not None
        first, rest = parts
        self.assertEqual((len(first.rows), len(rest.rows), first.first, rest.first), (3, 1, True, False))

    def test_a_total_row_never_starts_a_part_alone(self) -> None:
        """When only the total row would go on, the row above it goes with it."""
        card = Card.of(rows(3, total=True))
        parts = card.split(card.height - card.rows[-1].height)
        assert parts is not None
        self.assertEqual([len(part.rows) for part in parts], [2, 2])

    def test_a_card_does_not_split_when_no_row_fits_or_every_row_does(self) -> None:
        """Without room for one row, or with room for all, there is nothing to split."""
        card = Card.of(rows(3, total=False))
        self.assertIsNone(card.split(card.min_height - 1))
        self.assertIsNone(card.split(card.height + 1))


class PagesTest(unittest.TestCase):
    """Blocks flow onto pages in the order they were added."""

    def test_a_gap_is_left_out_at_the_top_of_a_page(self) -> None:
        """A block that doesn't fit starts the next page at its top, without the gap above it."""
        report = Report("Title", "Subtitle", DATE, "Footer")
        first, second = Filler(BOTTOM - BANNER - GAP), Filler(10)
        report.add(first)
        report.add(second, 50)
        self.assertEqual(pdf_facts(report.pdf())["pages"], 2)
        self.assertEqual((first.drawn, second.drawn), ([BANNER + GAP], [TOP]))

    def test_keep_holds_a_block_on_the_page_where_the_next_one_starts(self) -> None:
        """A block that fits but keeps a next block that doesn't goes to the next page with it."""
        report = Report("Title", "Subtitle", DATE, "Footer")
        filler, heading, block = Filler(BOTTOM - BANNER - GAP - 30), Filler(20), Filler(100)
        report.add(filler)
        report.add(heading, keep=True)
        report.add(block)
        report.pdf()
        self.assertEqual((heading.drawn, block.drawn), ([TOP], [TOP + 20]))

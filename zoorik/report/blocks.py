"""The report's blocks, and the report that flows them onto A4 pages.

A block has a height and draws itself from its top-left corner. A card that holds a table also splits between its
rows, so a long table goes on over pages, each part repeating the column titles.
"""

from dataclasses import dataclass, replace
from typing import List, Optional, Protocol, Sequence, Tuple, Union

from zoorik.report.pdf import (
    BLUE,
    BONE,
    DEEP,
    GREEN_INK,
    HAZE,
    INK,
    LILAC,
    MINT,
    MM,
    MUTED,
    NAVY,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    PANEL,
    PX,
    RULE,
    RULE2,
    SEPARATOR,
    WHITE,
    Align,
    Box,
    LineMetrics,
    Paragraph,
    Pdf,
    Spans,
    Style,
    line_metrics,
    paragraph,
    text_width,
)

# The page's side margins, and the width of the content between them.
SIDE = 16 * MM
WIDTH = PAGE_WIDTH - 2 * SIDE
# The space above a block: above a section, between cards, and between a section's heading and its first block.
GAP, CARD_GAP, SECTION_GAP = 7 * MM, 4 * MM, 3 * MM
# Blocks start this far down a later page, below its bar, and end this far down every page, above its footer.
TOP, BOTTOM = 24 * MM, PAGE_HEIGHT - 16 * MM
# Page 1's navy banner: its padding, the logo, and the title and subtitle lines.
BANNER = 25.5 * MM + 57.75
# The room for blocks on a page.
_ROOM = BOTTOM - TOP

# Column titles, the muted line under a cell's text, and a legend's labels.
_SMALL = Style(7.125, MUTED)
_SMALL_LINES = LineMetrics(8.25, 6.75)
_KPI_LABEL = Style(7.875, MUTED)
_KPI_LABEL_LINES = line_metrics(7.875, 1.4)
_TITLE = Style(8.25, INK, True)
_ASIDE = Style(8.25, MUTED)
_NAME = Style(10.5, INK, True)
_META = Style(7.5, MUTED)
_PILL = Style(8.25, GREEN_INK, True)
# A wrapped name's lines, and the meta's lines under them.
_NAME_LINES = LineMetrics(12.75, 10.5)
_META_LINES = LineMetrics(9.0, 7.5)
_FOOTER = Style(6.375, MUTED)
_BAR = Style(7.125, LILAC, spacing=0.1425)


class Block(Protocol):
    """What a report flows onto its pages: a height, and a drawing from a top-left corner."""

    @property
    def height(self) -> float:
        """Give the block's height, in points."""

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Draw the block with its top-left corner at (x, y)."""


class Art(Protocol):
    """A drawing in a table cell, under the cell's text."""

    def height(self, width: float) -> float:
        """Give the art's height when it is width points wide."""

    def draw(self, pdf: Pdf, x: float, y: float, width: float) -> None:
        """Draw the art width points wide, with its top-left corner at (x, y)."""


def text(spans: Spans, style: Style, line_height: Optional[float] = None) -> Paragraph:
    """Wrap spans in style across the content's width.

    line_height is the line's height as a multiple of the size; None is the font's own height.
    """
    return paragraph(spans, style, WIDTH, line_metrics(style.size, line_height))


def headline(spans: Spans) -> Paragraph:
    """Set spans as the headline that opens page 1."""
    return text(spans, Style(27, INK, True, -0.675), 1.12)


def fine(spans: Spans) -> Paragraph:
    """Set spans as the fine print that ends the report."""
    return text(spans, Style(7.125, MUTED), 1.5)


@dataclass(frozen=True)
class Kpi:
    """One KPI: a figure in its colour over a muted label; a newline in the label starts its second line."""

    value: str
    label: str
    color: str = INK


class Kpis:
    """A row of KPI cards of one width, each as tall as the tallest."""

    def __init__(self, kpis: Sequence[Kpi]) -> None:
        """Lay out kpis across the content's width, 6 points apart."""
        self._kpis = tuple(kpis)
        self._width = (WIDTH - 6 * (len(self._kpis) - 1)) / len(self._kpis)
        self._labels = [paragraph(kpi.label, _KPI_LABEL, self._width - 19.5, _KPI_LABEL_LINES) for kpi in self._kpis]

    @property
    def height(self) -> float:
        """Give the cards' height: the figure, then the label with the most lines."""
        return 42 + max(label.height for label in self._labels)

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Draw the cards from left to right, the row's top-left corner at (x, y)."""
        for index, (kpi, label) in enumerate(zip(self._kpis, self._labels)):
            left = x + index * (self._width + 6)
            pdf.rect(Box(left + PX / 2, y + PX / 2, self._width - PX, self.height - PX), PANEL, RULE, 8.625)
            pdf.text(left + 9.75, y + 25.5, kpi.value, Style(16.5, kpi.color, True, -0.33))
            label.draw(pdf, left + 9.75, y + 33)


@dataclass(frozen=True)
class Section:
    """A section's heading: its number in a blue badge, then its title."""

    number: int
    title: str

    @property
    def height(self) -> float:
        """Give the heading's height: its badge's."""
        return 19.5

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Draw the heading with its top-left corner at (x, y)."""
        pdf.rect(Box(x, y, 19.5, 19.5), BLUE, radius=9.75)
        pdf.text(x + 9.75, y + 13.5, str(self.number), Style(9.75, WHITE, True), "c")
        pdf.text(x + 27, y + 15.375, self.title, Style(15.75, INK, True, -0.236))


@dataclass(frozen=True)
class Key:
    """A legend's key: a swatch in fill, width by height points, outlined in green dashes when dashed."""

    label: str
    fill: str
    width: float = 10.5
    height: float = 5.25
    dashed: bool = False


@dataclass(frozen=True)
class Legend:
    """One line of keys, each a swatch and its label."""

    keys: Sequence[Key]

    @property
    def height(self) -> float:
        """Give the line's height."""
        return 8.25

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Draw the keys from left to right, the line's top-left corner at (x, y)."""
        for key in self.keys:
            # Each swatch stands on the labels' baseline.
            swatch = Box(x, y + 6.75 - key.height, key.width, key.height)
            pdf.rect(swatch, key.fill, radius=1.5)
            if key.dashed:
                pdf.dashed(swatch, 1.5)
            x += key.width + 3.75
            x += pdf.text(x, y + 6.75, key.label, _SMALL) + 10.5


@dataclass(frozen=True)
class Cell:
    """A table cell: its text, a muted line under it, and art under both; None keeps the row's weight and colour."""

    text: str = ""
    sub: str = ""
    bold: Optional[bool] = None
    color: Optional[str] = None
    art: Optional[Art] = None


@dataclass(frozen=True)
class Column:
    """A table's column: its share of the card's inner width, and how its title and its cells align."""

    share: float
    align: Align = "l"


# A table's row: in each column a cell, or a string for a cell of text alone.
Row = Sequence[Union[str, Cell]]


@dataclass(frozen=True)
class Table:
    """A table: its columns, their titles (a newline breaks one), its rows, and a bold total row under a rule."""

    columns: Sequence[Column]
    head: Sequence[str]
    rows: Sequence[Row]
    total: Optional[Row] = None


@dataclass(frozen=True)
class Padding:
    """The space inside a card's edge: above its content, at its sides and below it."""

    top: float
    side: float
    bottom: float


# A card sits closer around a name than around a title.
_PADDING = Padding(9.0, 10.5, 9.0)
_NAME_PADDING = Padding(7.5, 9.0, 3.0)


class Opening(Protocol):
    """The top of a card, laid out at the card's inner width."""

    @property
    def height(self) -> float:
        """Give its height, in points."""

    def draw(self, pdf: Pdf, left: float, right: float, y: float) -> None:
        """Draw it between left and right, its top at y."""


class Heading(Protocol):
    """What opens a card: a title or a name."""

    @property
    def padding(self) -> Padding:
        """Give the padding of a card that opens with it."""

    def lay_out(self, inner: float) -> Opening:
        """Lay it out at the card's inner width."""


@dataclass(frozen=True)
class Title:
    """A card's bold title, with a muted aside at its right end."""

    title: str
    aside: str = ""

    @property
    def padding(self) -> Padding:
        """Give the padding of a card that opens with a title."""
        return _PADDING

    @property
    def height(self) -> float:
        """Give the title's height."""
        return 16.5

    def lay_out(self, inner: float) -> "Title":
        """Give the title itself, which takes one line at any width."""
        return self

    def draw(self, pdf: Pdf, left: float, right: float, y: float) -> None:
        """Draw the title from left and the aside up to right, their baseline 8.25 points below y."""
        pdf.text(left, y + 8.25, self.title, _TITLE)
        pdf.text(right, y + 8.25, self.aside, _ASIDE, "r")


@dataclass(frozen=True)
class Name:
    """A card's bold name, then a muted meta line, with a green pill at its right end."""

    name: str
    meta: str = ""
    pill: str = ""

    @property
    def padding(self) -> Padding:
        """Give the padding of a card that opens with a name."""
        return _NAME_PADDING

    def lay_out(self, inner: float) -> "_NameOpening":
        """Set the name and the meta on one line beside the pill, or each wrapped in the room the pill leaves."""
        room = inner - (text_width(self.pill, _PILL) + 27 if self.pill else 0)
        # The name and the meta share one line, 6 points apart, while it holds them.
        if text_width(self.name, _NAME) + 6 + text_width(self.meta, _META) <= room:
            return _NameOpening(self, None)
        wrapped = (paragraph(self.name, _NAME, room, _NAME_LINES), paragraph(self.meta, _META, room, _META_LINES))
        return _NameOpening(self, wrapped)


@dataclass(frozen=True)
class _NameOpening:
    """A name laid out: on one line with its meta, or wrapped as its lines over the meta's."""

    name: Name
    wrapped: Optional[Tuple[Paragraph, Paragraph]]

    @property
    def height(self) -> float:
        """Give the opening's height: one line's, or the wrapped lines' when they are taller."""
        if self.wrapped is None:
            return 3 + 16.5
        name, meta = self.wrapped
        return 3 + max(16.5, 1.5 + name.height + meta.height)

    def draw(self, pdf: Pdf, left: float, right: float, y: float) -> None:
        """Draw the pill up to right, then the name and the meta from left, the opening's top at y."""
        pill = self.name.pill
        if pill:
            width = text_width(pill, _PILL) + 15
            pdf.rect(Box(right - width, y + 1.5, width, 15), MINT, radius=7.5)
            pdf.text(right - 7.5, y + 12, pill, _PILL, "r")
        if self.wrapped is None:
            width = pdf.text(left, y + 12, self.name.name, _NAME)
            pdf.text(left + width + 6, y + 12, self.name.meta, _META)
        else:
            name, meta = self.wrapped
            name.draw(pdf, left, y + 1.5)
            meta.draw(pdf, left, y + 1.5 + name.height)


@dataclass(frozen=True)
class _Slot:
    """A column placed in a card: its x from the content's left edge, its width and its alignment."""

    x: float
    width: float
    align: Align


def _cell_lines(text: str, style: Style, slot: _Slot, metrics: LineMetrics) -> Paragraph:
    """Wrap a cell's text in its column, inside the column's padding; an empty text takes no line."""
    if not text:
        return Paragraph((), metrics, slot.width - 9, slot.align)
    return paragraph(text, style, slot.width - 9, metrics, slot.align)


@dataclass(frozen=True)
class _LaidCell:
    """A cell laid out in its column: its text's lines, its muted line's, and its art, width points wide."""

    main: Paragraph
    sub: Paragraph
    art: Optional[Art]
    width: float

    @property
    def height(self) -> float:
        """Give the height of the cell's content."""
        return self.main.height + self.sub.height + (0.0 if self.art is None else self.art.height(self.width))

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Draw the text, the muted line under it and the art under both, from the content's top-left corner."""
        self.main.draw(pdf, x, y)
        y += self.main.height
        self.sub.draw(pdf, x, y)
        if self.art is not None:
            self.art.draw(pdf, x, y + self.sub.height, self.width)


@dataclass(frozen=True)
class _LaidRow:
    """A row laid out: its cells, and whether it is the total row."""

    cells: Tuple[_LaidCell, ...]
    total: bool

    @property
    def height(self) -> float:
        """Give the row's height: its tallest cell, with 6 points above and below."""
        return 12 + max(cell.height for cell in self.cells)


@dataclass(frozen=True)
class _Frame:
    """What each part of a card shares: its padding, its columns and their titles, and what opens its first part."""

    padding: Padding
    slots: Tuple[_Slot, ...]
    head: Tuple[Paragraph, ...]
    opening: Optional[Opening]
    legend: Optional[Legend]

    @property
    def head_lines(self) -> int:
        """Count the lines of the column title that has the most."""
        return max(len(title.lines) for title in self.head)

    @property
    def head_height(self) -> float:
        """Give the height of the column titles' row, with its padding."""
        return 8.25 * self.head_lines + 8.25

    @property
    def top_height(self) -> float:
        """Give the height of what opens the first part: the heading, then the legend."""
        heading = 0.0 if self.opening is None else self.opening.height
        return heading + (0.0 if self.legend is None else 16.75)

    def lay_out_row(self, cells: Row, total: bool) -> _LaidRow:
        """Lay out a row's cells in their columns; the total row's text is larger, and bold."""
        size = 9.0 if total else 8.25
        laid = []
        for given, slot in zip(cells, self.slots):
            cell = given if isinstance(given, Cell) else Cell(given)
            style = Style(size, cell.color or INK, total if cell.bold is None else cell.bold)
            main = _cell_lines(cell.text, style, slot, line_metrics(size))
            sub = _cell_lines(cell.sub, _SMALL, slot, _SMALL_LINES)
            laid.append(_LaidCell(main, sub, cell.art, slot.width - 9))
        return _LaidRow(tuple(laid), total)

    def draw_top(self, pdf: Pdf, left: float, right: float, y: float) -> float:
        """Draw what opens the first part between left and right from y, and give the y under it."""
        if self.opening is not None:
            self.opening.draw(pdf, left, right, y)
            y += self.opening.height
        if self.legend is not None:
            self.legend.draw(pdf, left, y)
            y += 16.75
        return y

    def draw_head(self, pdf: Pdf, left: float, right: float, y: float) -> float:
        """Draw the column titles from y, each at the foot of its row, and the rule under them; give the y under it."""
        for title, slot in zip(self.head, self.slots):
            title.draw(pdf, left + slot.x + 4.5, y + 3.75 + 8.25 * (self.head_lines - len(title.lines)))
        y += self.head_height
        pdf.rule(left, right, y - PX / 2, RULE)
        return y


@dataclass(frozen=True)
class Card:
    """A bordered card that holds a table, under a heading and a legend.

    A card taller than the room left on a page splits between its rows, and each part repeats the column titles.
    """

    frame: _Frame
    rows: Tuple[_LaidRow, ...]
    first: bool = True

    @classmethod
    def of(cls, table: Table, heading: Optional[Heading] = None, legend: Sequence[Key] = ()) -> "Card":
        """Lay out table in a card that opens with heading, then legend; a card may have neither."""
        padding = _PADDING if heading is None else heading.padding
        inner = WIDTH - 2 * (PX + padding.side)
        shares = [column.share for column in table.columns]
        edges = [inner * sum(shares[:index]) for index in range(len(shares) + 1)]
        slots = tuple(
            _Slot(left, right - left, column.align) for column, left, right in zip(table.columns, edges, edges[1:])
        )
        head = tuple(
            paragraph(title, _SMALL, slot.width - 9, _SMALL_LINES, slot.align) for title, slot in zip(table.head, slots)
        )
        opening = None if heading is None else heading.lay_out(inner)
        frame = _Frame(padding, slots, head, opening, Legend(legend) if legend else None)
        rows = [frame.lay_out_row(cells, total=False) for cells in table.rows]
        if table.total:
            rows.append(frame.lay_out_row(table.total, total=True))
        return cls(frame, tuple(rows))

    @property
    def height(self) -> float:
        """Give the card's height: its edge and padding, its opening on the first part, the titles and the rows."""
        padding = self.frame.padding
        return (
            2 * PX
            + padding.top
            + padding.bottom
            + (self.frame.top_height if self.first else 0.0)
            + self.frame.head_height
            + sum(row.height for row in self.rows)
            + PX * (len(self.rows) - 1)
        )

    @property
    def min_height(self) -> float:
        """Give the height of the card with its first row alone, the least of it that a page takes."""
        return self.height - sum(row.height + PX for row in self.rows[1:])

    def split(self, room: float) -> Optional[Tuple["Card", "Card"]]:
        """Split the card into the rows that fit in room and the rest; None when not one row fits, or all of them."""
        rows = self.rows
        used = self.height - sum(row.height + PX for row in rows) + PX
        count = 0
        while count < len(rows) and used + rows[count].height + (PX if count else 0.0) <= room:
            used = used + rows[count].height + (PX if count else 0.0)
            count += 1
        # A total row never starts a part alone: the row above it goes with it.
        if count == len(rows) - 1 and count > 1 and rows[-1].total:
            count -= 1
        if count in (0, len(rows)):
            return None
        return replace(self, rows=rows[:count]), replace(self, rows=rows[count:], first=False)

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Draw the card with its top-left corner at (x, y)."""
        frame = self.frame
        pdf.rect(Box(x + PX / 2, y + PX / 2, WIDTH - PX, self.height - PX), WHITE, RULE, 9 - PX / 2)
        left = x + PX + frame.padding.side
        right = x + WIDTH - PX - frame.padding.side
        top = y + PX + frame.padding.top
        if self.first:
            top = frame.draw_top(pdf, left, right, top)
        top = frame.draw_head(pdf, left, right, top)
        for index, row in enumerate(self.rows):
            for cell, slot in zip(row.cells, frame.slots):
                # A cell's content is centred in its row, on whole pixels.
                cell.draw(pdf, left + slot.x + 4.5, top + 6 + int((row.height - 12 - cell.height) / 2 / PX) * PX)
            top += row.height + PX
            if index + 1 < len(self.rows):
                # The rule above the total row is darker than the rules between the other rows.
                pdf.rule(left, right, top - PX / 2, RULE if self.rows[index + 1].total else RULE2)


class NextSteps:
    """The navy card that ends the report: a white title over a paragraph."""

    def __init__(self, title: str, spans: Spans) -> None:
        """Set title over spans, wrapped inside the card's padding."""
        self._title = title
        self._body = paragraph(spans, Style(8.25, HAZE), WIDTH - 27, LineMetrics(15.75, 11.75))

    @property
    def height(self) -> float:
        """Give the card's height: its padding, the title and the paragraph."""
        return 36 + self._body.height

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Draw the card with its top-left corner at (x, y)."""
        pdf.rect(Box(x, y, WIDTH, self.height), DEEP, radius=10.5)
        pdf.text(x + 13.5, y + 21, self._title, Style(10.5, WHITE, True))
        self._body.draw(pdf, x + 13.5, y + 25.5)


@dataclass(frozen=True)
class _Entry:
    """A block as the report was given it: the space above it, and whether it keeps the next block on its page."""

    block: Block
    gap: float
    keep: bool


def _splits(card: Card, room: float) -> bool:
    """Tell whether card splits in room: when it is taller than a page, or when a third of a page or more is left."""
    return card.height > _ROOM or room >= _ROOM / 3


def _split(block: Block, room: float) -> Optional[Tuple[Card, Card]]:
    """Split block in room when it is a card that splits there, into the part that fits and the rest."""
    if isinstance(block, Card) and _splits(block, room):
        return block.split(room)
    return None


def _fits_a_page(block: Block) -> bool:
    """Tell whether block goes whole at the top of a page: any block but a card taller than a page."""
    return not isinstance(block, Card) or block.height <= _ROOM


class _Pages:
    """Blocks flowed onto pages: pages holds each page's blocks, each with the y of its top."""

    def __init__(self, entries: Sequence[_Entry]) -> None:
        """Flow entries onto pages in order, from below page 1's banner."""
        self._entries = list(entries)
        self._y = BANNER + GAP
        self.pages: List[List[Tuple[Block, float]]] = [[]]
        index = 0
        while index < len(self._entries):
            index = self._place(index)

    def _place(self, index: int) -> int:
        """Place the block at index, or the part of it that fits, and give the index of the next block to place."""
        entry = self._entries[index]
        fresh = not self.pages[-1]
        # A block's gap is left out at the top of a page.
        gap = 0.0 if fresh else entry.gap
        if self._fits(index, self._y + gap) or (fresh and _fits_a_page(entry.block)):
            self.pages[-1].append((entry.block, self._y + gap))
            self._y = self._y + gap + entry.block.height
            return index + 1
        parts = _split(entry.block, BOTTOM - self._y - gap)
        if parts is not None:
            self.pages[-1].append((parts[0], self._y + gap))
            self._entries[index] = _Entry(parts[1], 0.0, entry.keep)
        elif fresh:
            # A card whose first row is taller than a page goes on a page of its own, whole.
            self.pages[-1].append((entry.block, self._y))
            index += 1
        self.pages.append([])
        self._y = TOP
        return index

    def _fits(self, index: int, top: float) -> bool:
        """Tell whether the block at index fits on this page from top, with the blocks it keeps on its page."""
        height = self._entries[index].block.height
        return top + height + self._kept(index, top + height) <= BOTTOM

    def _kept(self, index: int, at: float) -> float:
        """Measure the space from at that the next block needs, when the block at index keeps it on its page."""
        if self._entries[index].keep and index + 1 < len(self._entries):
            return self._need(index + 1, at)
        return 0.0

    def _need(self, index: int, at: float) -> float:
        """Measure the space from at that the block at index needs: its gap, itself, and the blocks it keeps.

        A card that would split there needs only its first row.
        """
        entry = self._entries[index]
        block = entry.block
        if isinstance(block, Card) and _splits(block, BOTTOM - at - entry.gap):
            height = block.min_height
        else:
            height = block.height
        return entry.gap + height + self._kept(index, at + entry.gap + height)


class Report:
    """The report as A4 pages: page 1 opens with the navy banner and later pages with a slim bar; each has a footer."""

    def __init__(self, title: str, sub: str, date: str, footer: str) -> None:
        """Start a report: its title and subtitle in the banner, its date in each later bar, and its footer."""
        self.title = title
        self.sub = sub
        self.date = date
        self.footer = footer
        self._entries: List[_Entry] = []

    def add(self, block: Block, gap: float = 0.0, keep: bool = False) -> None:
        """Add block after the others.

        gap is the space above the block, left out at the top of a page; keep holds the block on the page where the
        next block starts.
        """
        self._entries.append(_Entry(block, gap, keep))

    def pdf(self) -> bytes:
        """Flow the blocks onto pages, and write the pages as a PDF."""
        pages = _Pages(self._entries).pages
        pdf = Pdf()
        for number, blocks in enumerate(pages, 1):
            pdf.new_page()
            if number == 1:
                self._draw_banner(pdf)
            else:
                self._draw_bar(pdf)
            for block, y in blocks:
                block.draw(pdf, SIDE, y)
            pdf.text(SIDE, PAGE_HEIGHT - 14.7, self.footer, _FOOTER)
            pdf.text(PAGE_WIDTH - SIDE, PAGE_HEIGHT - 14.7, f"{number} / {len(pages)}", _FOOTER, "r")
        return pdf.save("Zoorik " + self.title)

    def _draw_banner(self, pdf: Pdf) -> None:
        """Draw page 1's navy banner: the logo over the title and the subtitle."""
        pdf.rect(Box(0, 0, PAGE_WIDTH, BANNER), NAVY)
        pdf.logo(SIDE, 10 * MM, 96)
        pdf.text(SIDE, 88.11, self.title, Style(19.5, BONE, True, -0.39))
        pdf.text(SIDE, 105.11, self.sub, Style(8.25, LILAC))

    def _draw_bar(self, pdf: Pdf) -> None:
        """Draw a later page's slim navy bar: the logo, then the title and the date at its right end."""
        pdf.rect(Box(0, 0, PAGE_WIDTH, 16 * MM), NAVY)
        pdf.logo(SIDE, 8 * MM - 9, 72)
        x = PAGE_WIDTH - SIDE - text_width(self.title + SEPARATOR + self.date, _BAR) - 9
        for part in (self.title, SEPARATOR, self.date):
            x += pdf.text(x, 25.5, part, _BAR) + 4.5

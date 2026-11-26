"""The PDF engine: A4 pages in the standard Helvetica fonts, with text measured and wrapped, written as one PDF file.

It draws the report's design as a browser lays it out, in points (1/72 inch). Nothing is embedded: the fonts are the
standard ones every PDF reader has, and the logo is drawn as paths.
"""

import math
import re
import unicodedata
import zlib
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Literal, Optional, Sequence, Tuple, Union

MM = 72 / 25.4
# One pixel of the design, in points.
PX = 0.75
PAGE_WIDTH, PAGE_HEIGHT = 210 * MM, 297 * MM

# The design's colours.
INK, MUTED, RULE, RULE2, PANEL = "#0F1222", "#6B7086", "#E4E7F2", "#EFF1F8", "#F6F7FB"
NAVY, DEEP, BONE, LILAC, HAZE, UNDERLINE = "#050B3D", "#070F52", "#FAF7F3", "#D9D6EE", "#D6DAF5", "#8E98EB"
BLUE, GREEN, GREEN_INK, MINT, WHITE = "#3B5BF5", "#12A97A", "#117D60", "#E3F5EF", "#FFFFFF"

# Where a line of text sits on x: from its left end, its centre or its right end.
Align = Literal["l", "c", "r"]
# The middle dot between the items of a line, such as a cluster's platform and its region.
SEPARATOR = "\xb7"

# The Adobe AFM advance widths of the WinAnsi codes from _FIRST_CODE to 255, in 1/1000 em.
_FIRST_CODE = 32
# fmt: off
_HELVETICA = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556,
    556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556,
    833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500,
    556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500,
    334, 260, 334, 584, 350, 556, 350, 222, 556, 333, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350, 350,
    222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 350, 500, 667, 278, 333, 556, 556, 556, 556, 260,
    556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834,
    834, 611, 667, 667, 667, 667, 667, 667, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778,
    778, 778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556,
    556, 278, 278, 278, 278, 556, 556, 556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500,
)
_HELVETICA_BOLD = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556,
    556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833,
    722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611,
    556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389,
    280, 389, 584, 350, 556, 350, 278, 556, 500, 1000, 556, 556, 333, 1000, 667, 333, 1000, 350, 611, 350, 350, 278,
    278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 350, 500, 667, 278, 333, 556, 556, 556, 556, 280, 556,
    333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333, 333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834,
    611, 722, 722, 722, 722, 722, 722, 1000, 722, 667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778,
    778, 778, 584, 778, 722, 722, 722, 722, 667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556,
    278, 278, 278, 278, 611, 611, 611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556,
)
# fmt: on
# Code page 1252 as the fonts' WinAnsi encoding: each character it holds, to the character whose code point is its
# byte. Decoding leaves the code page's five undefined bytes as U+FFFD, which is not one of its characters.
_WINANSI = {
    char: chr(code) for code, char in enumerate(bytes(range(256)).decode("cp1252", "replace")) if char != "\ufffd"
}
# The ascent and descent of the design's font, in em; the browser rounds each to whole pixels.
_ASCENT, _DESCENT = 0.96875, 0.2421875
# A quarter circle drawn as one Bezier curve has its control points this share of the radius from the corner.
_CORNER = 0.4477
# The green dashed outline: 1.5 pixels wide, its dashes about 7.2 widths apart, each dash 62% of that.
_DASH_WIDTH = 1.5 * PX
_DASH_PERIOD, _DASH_ON = 7.2, 0.62
# A line may run this far past its width, so text that fits exactly is not broken by rounding.
_SLACK = 0.01
# A newline, a run of spaces, a part of a word that ends in a hyphen, or the rest of a word.
_TOKENS = re.compile(r"\n| +|[^ \n-]*-|[^ \n-]+")
# The bone logo as PDF paths, in its SVG's units, y up.
_LOGO_WIDTH, _LOGO_HEIGHT = 1725, 430
_LOGO = (
    "180 380 m 205 404 l 229 380 l 205 355 l h 78 329 m 84 370 l 126 363 l 119 322 l h 30 235 m 15 266 l 46 282 "
    "l 62 251 l h 47 122 m 10 141 l 29 178 l 66 159 l h 122 48 m 87 43 l 82 77 l 116 83 l h 234 29 m 205 0 l "
    "175 29 l 205 59 l h 328 77 m 322 43 l 288 48 l 293 83 l h 380 178 m 399 141 l 362 122 l 343 159 l h 364 "
    "282 m 395 266 l 379 235 l 348 251 l h 284 363 m 325 370 l 332 329 l 290 322 l h 185 325 m 205 345 l 225 "
    "325 l 205 305 l h 84 222 m 71 248 l 96 261 l 109 235 l h 150 95 m 122 91 l 118 119 l 146 123 l h 292 119 m "
    "287 91 l 259 95 l 264 123 l h 313 261 m 338 248 l 326 222 l 300 235 l h 291 204 m 291 156 253 118 205 118 "
    "c 157 118 118 156 118 204 c 118 252 157 291 205 291 c 253 291 291 252 291 204 c h 718 316 m 718 277 l 609 "
    "135 l 720 135 l 714 92 l 547 92 l 547 131 l 656 274 l 556 274 l 556 316 l h 953 205 m 953 181 949 160 940 "
    "142 c 932 124 920 111 904 101 c 888 91 869 86 848 86 c 815 86 789 96 770 117 c 751 138 742 168 742 205 c "
    "742 228 746 249 755 266 c 763 284 776 298 792 308 c 807 318 826 323 848 323 c 881 323 907 312 925 291 c "
    "944 270 953 241 953 205 c h 799 205 m 799 178 803 158 811 146 c 819 133 832 127 848 127 c 864 127 876 133 "
    "884 146 c 892 159 896 178 896 205 c 896 231 892 250 884 263 c 876 275 864 282 848 282 c 816 282 799 256 "
    "799 205 c h 1199 205 m 1199 181 1194 160 1186 142 c 1177 124 1165 111 1149 101 c 1134 91 1115 86 1093 86 c "
    "1060 86 1034 96 1016 117 c 997 138 988 168 988 205 c 988 228 992 249 1000 266 c 1009 284 1021 298 1037 308 "
    "c 1053 318 1072 323 1094 323 c 1127 323 1152 312 1171 291 c 1189 270 1199 241 1199 205 c h 1045 205 m 1045 "
    "178 1049 158 1057 146 c 1065 133 1077 127 1093 127 c 1109 127 1121 133 1129 146 c 1137 159 1141 178 1141 "
    "205 c 1141 231 1137 250 1129 263 c 1121 275 1109 282 1094 282 c 1061 282 1045 256 1045 205 c h 1380 319 m "
    "1371 267 l 1364 269 1358 269 1351 269 c 1338 269 1327 265 1320 255 c 1312 246 1306 231 1302 212 c 1302 92 "
    "l 1247 92 l 1247 316 l 1294 316 l 1299 272 l 1305 288 1313 301 1323 309 c 1333 318 1345 322 1358 322 c "
    "1366 322 1374 321 1380 319 c h 1466 316 m 1466 92 l 1412 92 l 1412 316 l h 1473 397 m 1473 388 1470 380 "
    "1464 374 c 1457 367 1449 364 1439 364 c 1429 364 1421 367 1415 374 c 1408 380 1405 388 1405 397 c 1405 406 "
    "1408 414 1415 420 c 1421 427 1429 430 1439 430 c 1449 430 1457 427 1464 420 c 1470 414 1473 406 1473 397 c "
    "h 1584 412 m 1584 92 l 1530 92 l 1530 406 l h 1718 316 m 1644 218 l 1725 92 l 1663 92 l 1586 215 l 1661 "
    "316 l h f"
)
_FONTS = ("Helvetica /Encoding /WinAnsiEncoding", "Helvetica-Bold /Encoding /WinAnsiEncoding", "Symbol")
# Objects 1 to 6 are the catalog, the page tree, the info and the three fonts; each page then takes two objects.
_FIRST_PAGE = 7
# The version, then a comment of four bytes above 127, which marks the file as binary.
_HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


@dataclass(frozen=True)
class Style:
    """How text is set: its size in points, its colour, its weight, and the space added after each letter."""

    size: float
    color: str = INK
    bold: bool = False
    spacing: float = 0.0


@dataclass(frozen=True)
class Box:
    """A rectangle on the page: x from the left edge and y down from the top edge to its top-left corner, in points."""

    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class LineMetrics:
    """How lines of text stack: the pitch from one line's top to the next, and the baseline below a line's top."""

    pitch: float
    baseline: float


@dataclass(frozen=True)
class _Drawn:
    """A character the fonts' WinAnsi lacks, drawn as art over its glyph in the Symbol font (F3), which stays unseen.

    The unseen glyph keeps the character in the text a reader copies or searches, and the art matches Helvetica.
    """

    code: str
    symbol_width: int
    width: int
    art: Callable[[float, float, Style], str]


def _minus(x: float, baseline: float, style: Style) -> str:
    """Give the operators that draw Helvetica's own minus sign as a bar, from x on the baseline (y up)."""
    left, bottom, right, top = (40, 197, 544, 309) if style.bold else (39, 216, 545, 289)
    size = style.size
    return (
        f" {_rgb(style.color)} rg {_number(x + left * size / 1000)} {_number(baseline + bottom * size / 1000)} "
        f"{_number((right - left) * size / 1000)} {_number((top - bottom) * size / 1000)} re f"
    )


def _arrow(x: float, baseline: float, style: Style) -> str:
    """Give the operators that draw an arrow across the figures, as heavy as Helvetica, from x on the baseline."""
    size = style.size
    stroke = (0.112 if style.bold else 0.073) * size
    y = baseline + 0.34 * size
    head = 0.29 * size
    # The head's mitred corner reaches past its point by the stroke over the square root of 2.
    tip = x + 0.82 * size - stroke * 0.7071
    return (
        f" q {_rgb(style.color)} RG {_number(stroke)} w 0 J 0 j {_number(x + 0.08 * size)} {_number(y)} m "
        f"{_number(tip)} {_number(y)} l S {_number(tip - head)} {_number(y + head)} m {_number(tip)} {_number(y)} l "
        f"{_number(tip - head)} {_number(y - head)} l S Q"
    )


# The arrow and the minus sign: their Symbol codes and glyph widths, and the widths they take, in 1/1000 em.
_DRAWN: Dict[str, _Drawn] = {"\u2192": _Drawn("\xae", 987, 900, _arrow), "\u2212": _Drawn("-", 549, 584, _minus)}
_DRAWN_RUNS = re.compile("[\u2192\u2212]|[^\u2192\u2212]+")


def to_winansi(text: str) -> str:
    """Give text in the WinAnsi code points the fonts draw; the arrow and the minus sign stay as they are.

    A character WinAnsi lacks becomes the first character of its compatibility decomposition when WinAnsi has that
    (a plain a for an a with a macron), else a question mark. A control character becomes a space.
    """
    return "".join(_winansi(char) for char in text)


def _winansi(char: str) -> str:
    """Give the WinAnsi code point that draws char."""
    if " " <= char <= "~" or char in _DRAWN:
        return char
    for candidate in (char, unicodedata.normalize("NFKD", char)[:1]):
        if candidate < " " or candidate == "\x7f":
            return " "
        if candidate in _WINANSI:
            return _WINANSI[candidate]
    return "?"


def text_width(text: str, style: Style) -> float:
    """Measure text as it is drawn in style, in points."""
    return _advance(to_winansi(text), style)


def _advance(codes: str, style: Style) -> float:
    """Measure text that is already in WinAnsi code points, in points."""
    widths = _HELVETICA_BOLD if style.bold else _HELVETICA
    em = sum(_DRAWN[code].width if code in _DRAWN else widths[ord(code) - _FIRST_CODE] for code in codes)
    return em * style.size / 1000 + style.spacing * len(codes)


def line_metrics(size: float, line_height: Optional[float] = None) -> LineMetrics:
    """Give the metrics of lines of text in size, as the design's browser lays a line out.

    line_height is the line's height as a multiple of size; None is the font's own height, its ascent and descent.
    """
    ascent = int(size / PX * _ASCENT + 0.5) * PX
    descent = int(size / PX * _DESCENT + 0.5) * PX
    pitch = ascent + descent if line_height is None else int(size * line_height / PX) * PX
    # The browser centres the font's height in the line's.
    return LineMetrics(pitch, (pitch - ascent - descent) / 2 + ascent)


def _share(length: float, align: Align) -> float:
    """Give the part of length that align moves a line by: none of it for l, half of it for c and all of it for r."""
    if align == "r":
        return length
    if align == "c":
        return length / 2
    return 0.0


def _number(value: float, digits: int = 2) -> str:
    """Write a number as a PDF operand: at most digits decimals, without trailing zeros or a trailing point."""
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _rgb(color: str) -> str:
    """Write a #RRGGBB colour as the three fractions a PDF colour operator takes."""
    return " ".join(_number(int(color[index : index + 2], 16) / 255, 3) for index in (1, 3, 5))


def _escape(text: str) -> str:
    """Escape the backslashes and brackets of a PDF string."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _operation(operator: str, *operands: float) -> str:
    """Write one PDF operation: its operands, then its operator."""
    return " ".join(map(_number, operands)) + " " + operator


def _path(left: float, bottom: float, width: float, height: float, radius: float) -> str:
    """Give the path of a rectangle from its bottom-left in page coordinates (y up), its corners rounded by radius."""
    # A corner can't be rounder than half of the shorter side.
    radius = max(0.0, min(radius, width / 2, height / 2))
    if not radius:
        return _operation("re", left, bottom, width, height)
    corner, right, top = radius * _CORNER, left + width, bottom + height
    return " ".join(
        (
            _operation("m", left + radius, bottom),
            _operation("l", right - radius, bottom),
            _operation("c", right - corner, bottom, right, bottom + corner, right, bottom + radius),
            _operation("l", right, top - radius),
            _operation("c", right, top - corner, right - corner, top, right - radius, top),
            _operation("l", left + radius, top),
            _operation("c", left + corner, top, left, top - corner, left, top - radius),
            _operation("l", left, bottom + radius),
            _operation("c", left, bottom + corner, left + corner, bottom, left + radius, bottom),
        )
    )


@dataclass
class _Page:
    """One page as it is drawn: its drawing operators and its link annotations."""

    ops: List[str] = field(default_factory=list)
    annots: List[str] = field(default_factory=list)

    def objects(self, contents: int) -> Tuple[bytes, bytes]:
        """Give the page's two objects: the page, whose contents are object number contents, and those contents."""
        data = zlib.compress("\n".join(self.ops).encode("latin-1"))
        annots = f" /Annots [{' '.join(self.annots)}]" if self.annots else ""
        page = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_number(PAGE_WIDTH)} {_number(PAGE_HEIGHT)}] "
            f"/Contents {contents} 0 R /Resources << /Font << /F1 4 0 R /F2 5 0 R /F3 6 0 R >> >>{annots} >>"
        )
        stream = f"<< /Length {len(data)} /Filter /FlateDecode >>\nstream\n".encode() + data + b"\nendstream"
        return page.encode(), stream


class Pdf:
    """A4 pages of drawing operators. x runs from the left edge and y down from the top edge, in points."""

    def __init__(self) -> None:
        """Start with no page; new_page starts each one."""
        self._pages: List[_Page] = []

    def new_page(self) -> None:
        """Start a page; what is drawn from now on goes on it."""
        self._pages.append(_Page())

    @property
    def _ops(self) -> List[str]:
        """Give the drawing operators of the page being drawn."""
        return self._pages[-1].ops

    def text(self, x: float, y: float, text: str, style: Style, align: Align = "l") -> float:
        """Draw one line of text with its baseline at y, aligned on x by align, and give its width."""
        codes = to_winansi(text)
        width = _advance(codes, style)
        x -= _share(width, align)
        baseline = PAGE_HEIGHT - y
        size = _number(style.size)
        runs = []
        art = []
        for run in _DRAWN_RUNS.findall(codes):
            drawn = _DRAWN.get(run)
            at = f"1 0 0 1 {_number(x)} {_number(baseline)} Tm"
            if drawn is None:
                runs.append(f"{at} /F{2 if style.bold else 1} {size} Tf ({_escape(run)}) Tj")
            else:
                scale = _number(100 * drawn.width / drawn.symbol_width)
                runs.append(f"{at} 3 Tr {scale} Tz /F3 {size} Tf ({drawn.code}) Tj 0 Tr 100 Tz")
                art.append(drawn.art(x, baseline, style))
            x += _advance(run, style)
        # An empty line draws nothing.
        if codes:
            spacing = _number(style.spacing)
            self._ops.append(f"BT {_rgb(style.color)} rg {spacing} Tc {' '.join(runs)} ET{''.join(art)}")
        return width

    def rect(self, box: Box, fill: str, stroke: Optional[str] = None, radius: float = 0.0) -> None:
        """Fill box, with a hairline edge in stroke when it is given, its corners rounded by radius."""
        edge = f"{_rgb(stroke)} RG {_number(PX)} w " if stroke else ""
        path = _path(box.x, PAGE_HEIGHT - box.y - box.height, box.width, box.height, radius)
        self._ops.append(f"q {_rgb(fill)} rg {edge}{path} {'B' if stroke else 'f'} Q")

    def dashed(self, box: Box, radius: float) -> None:
        """Draw a green dashed outline just inside box, its corners rounded by radius, its dashes spread evenly."""
        width, height = box.width - _DASH_WIDTH, box.height - _DASH_WIDTH
        radius = max(0.0, min(radius - _DASH_WIDTH / 2, width / 2, height / 2))
        around = 2 * (width + height) - (8 - 2 * math.pi) * radius
        # An even number of dashes, at least 8, so each side gets the same.
        period = around / max(8, 2 * round(around / (_DASH_PERIOD * _DASH_WIDTH)))
        on = period * _DASH_ON
        # The phase centres a dash on the middle of the bottom side.
        phase = period - (width / 2 - radius - on / 2) % period
        path = _path(box.x + _DASH_WIDTH / 2, PAGE_HEIGHT - box.y - height - _DASH_WIDTH / 2, width, height, radius)
        self._ops.append(
            f"q {_rgb(GREEN)} RG {_number(_DASH_WIDTH)} w [{_number(on)} {_number(period - on)}] {_number(phase)} d "
            f"{path} S Q"
        )

    def rule(self, x1: float, x2: float, y: float, color: str) -> None:
        """Draw a hairline across from x1 to x2 at y."""
        across = _number(PAGE_HEIGHT - y)
        self._ops.append(f"q {_rgb(color)} RG {_number(PX)} w {_number(x1)} {across} m {_number(x2)} {across} l S Q")

    def link(self, box: Box, url: str) -> None:
        """Make box a link to url."""
        corners = (box.x, PAGE_HEIGHT - box.y - box.height, box.x + box.width, PAGE_HEIGHT - box.y)
        self._pages[-1].annots.append(
            f"<< /Type /Annot /Subtype /Link /Rect [{' '.join(map(_number, corners))}] /Border [0 0 0] "
            f"/A << /S /URI /URI ({_escape(url)}) >> >>"
        )

    def logo(self, x: float, y: float, width: float) -> None:
        """Draw the bone Zoorik logo width points wide, from its top-left corner (x, y)."""
        scale = width / _LOGO_WIDTH
        self._ops.append(
            f"q {_rgb(BONE)} rg {_number(scale)} 0 0 {_number(scale)} {_number(x)} "
            f"{_number(PAGE_HEIGHT - y - _LOGO_HEIGHT * scale)} cm {_LOGO} Q"
        )

    def save(self, title: str) -> bytes:
        """Write the document: the catalog, the page tree, the info, the three standard fonts, then each page."""
        kids = " ".join(f"{_FIRST_PAGE + 2 * index} 0 R" for index in range(len(self._pages)))
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R /ViewerPreferences << /DisplayDocTitle true >> >>",
            f"<< /Type /Pages /Kids [{kids}] /Count {len(self._pages)} >>".encode(),
            f"<< /Title <FEFF{title.encode('utf-16-be').hex().upper()}> /Producer (Zoorik) >>".encode(),
        ]
        objects.extend(f"<< /Type /Font /Subtype /Type1 /BaseFont /{font} >>".encode() for font in _FONTS)
        for page in self._pages:
            objects.extend(page.objects(contents=len(objects) + 2))
        return _file(objects)


def _file(objects: Sequence[bytes]) -> bytes:
    """Write objects as a PDF file: the header, each object numbered from 1, the cross-references and the trailer."""
    out = bytearray(_HEADER)
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@dataclass(frozen=True)
class Span:
    """A part of a paragraph in a weight or a colour of its own, or a link to url; None keeps the paragraph's."""

    text: str
    bold: Optional[bool] = None
    color: Optional[str] = None
    url: Optional[str] = None


# A paragraph's text: one string, or strings and spans one after another.
Spans = Union[str, Sequence[Union[str, Span]]]


@dataclass(frozen=True)
class Piece:
    """A run of text on a wrapped line: x from the line's start, its text and style, and the link it makes."""

    x: float
    text: str
    style: Style
    url: Optional[str]


@dataclass(frozen=True)
class Line:
    """A wrapped line: its width, and its pieces from left to right."""

    width: float
    pieces: Tuple[Piece, ...]


@dataclass(frozen=True)
class _Fragment:
    """A word or a part of one in one span's style and link, with its width."""

    text: str
    style: Style
    url: Optional[str]
    width: float


@dataclass
class _Unit:
    """What no line breaks inside: the spaces before it, and the fragments of every span that no space parts."""

    spaces: int
    fragments: List[_Fragment]

    @property
    def width(self) -> float:
        """Give the width of the fragments, in points."""
        return sum(fragment.width for fragment in self.fragments)


def _units(spans: Spans, style: Style) -> List[List[_Unit]]:
    """Cut spans at their newlines into paragraphs, and each paragraph into units: words, or parts up to a hyphen."""
    paragraphs: List[List[_Unit]] = [[]]
    spaces, joined = 0, False
    for part in [spans] if isinstance(spans, str) else spans:
        span = part if isinstance(part, Span) else Span(part)
        bold = style.bold if span.bold is None else span.bold
        fragment_style = replace(style, bold=bold, color=span.color or style.color)
        for token in _TOKENS.findall(span.text):
            if token == "\n":
                paragraphs.append([])
                spaces, joined = 0, False
            elif token[0] == " ":
                spaces, joined = spaces + len(token), False
            else:
                fragment = _Fragment(token, fragment_style, span.url, text_width(token, fragment_style))
                if joined:
                    paragraphs[-1][-1].fragments.append(fragment)
                else:
                    paragraphs[-1].append(_Unit(spaces, [fragment]))
                # A line may break after a hyphen, so the rest of the word starts a unit of its own.
                spaces, joined = 0, not token.endswith("-")
    return paragraphs


def _word_width(units: Sequence[_Unit], index: int) -> float:
    """Measure the word that the unit at index starts, with the parts after its hyphens; any other unit alone."""
    word = units[index].width
    if units[index].spaces or index == 0:
        for unit in units[index + 1 :]:
            if unit.spaces:
                break
            word += unit.width
    return word


class _Lines:
    """Fills lines from left to right, each no wider than its width; done holds the lines filled so far."""

    def __init__(self, width: float, space: float) -> None:
        """Fill lines width points wide, in which a space is space points wide."""
        self.done: List[Line] = []
        self._width = width
        self._space = space
        self._pieces: List[Piece] = []
        self._x = 0.0

    def fill(self, units: Sequence[_Unit]) -> None:
        """Fill lines with one paragraph's units, then end its last line."""
        for index, unit in enumerate(units):
            self._add(unit, _word_width(units, index))
        self._end_line()

    def _add(self, unit: _Unit, word: float) -> None:
        """Add a unit after its spaces, starting a new line when it doesn't fit on this one.

        word is the width of the word the unit starts, or the unit's own width when it goes on with a word after a
        hyphen. A word that fits a line moves to the next line whole; a wider one breaks after a hyphen, and a unit
        wider than a line breaks between its letters.
        """
        gap = unit.spaces * self._space if self._pieces else 0.0
        width = unit.width
        if self._pieces and self._x + gap + (word if word <= self._width else width) > self._width + _SLACK:
            self._end_line()
            gap = 0.0
        self._x += gap
        if width <= self._width:
            self._add_whole(unit)
        else:
            self._add_letters(unit)

    def _add_whole(self, unit: _Unit) -> None:
        """Add a unit's fragments one after another."""
        for fragment in unit.fragments:
            self._pieces.append(Piece(self._x, fragment.text, fragment.style, fragment.url))
            self._x += fragment.width

    def _add_letters(self, unit: _Unit) -> None:
        """Add a unit wider than a line letter by letter, going on to a new line where the next letter doesn't fit."""
        for fragment in unit.fragments:
            growing = False
            for letter in fragment.text:
                width = text_width(letter, fragment.style)
                if self._pieces and self._x + width > self._width + _SLACK:
                    self._end_line()
                    growing = False
                if growing:
                    self._pieces[-1] = replace(self._pieces[-1], text=self._pieces[-1].text + letter)
                else:
                    self._pieces.append(Piece(self._x, letter, fragment.style, fragment.url))
                    growing = True
                self._x += width

    def _end_line(self) -> None:
        """End the line being filled, and start an empty one."""
        self.done.append(Line(self._x, tuple(self._pieces)))
        self._pieces = []
        self._x = 0.0


def _without_end_dots(line: Line) -> Line:
    """Leave out a separator dot that wrapping left alone at either end of a line."""
    width, pieces = line.width, line.pieces
    # A line that is only a dot keeps it.
    if len(pieces) > 1 and pieces[-1].text == SEPARATOR:
        pieces = pieces[:-1]
        width = pieces[-1].x + text_width(pieces[-1].text, pieces[-1].style)
    if len(pieces) > 1 and pieces[0].text == SEPARATOR:
        shift = pieces[1].x
        width, pieces = width - shift, tuple(replace(piece, x=piece.x - shift) for piece in pieces[1:])
    return Line(width, pieces)


def wrap(spans: Spans, style: Style, width: float) -> List[Line]:
    """Wrap spans in style into lines no wider than width.

    Lines break at spaces and newlines. A word with hyphens stays whole when it fits a line, and breaks after a hyphen
    only when it doesn't; a part still wider than a line breaks between letters. A separator dot left at either end of
    a line is left out.
    """
    lines = _Lines(width, text_width(" ", Style(style.size, spacing=style.spacing)))
    for units in _units(spans, style):
        lines.fill(units)
    return [_without_end_dots(line) for line in lines.done]


@dataclass(frozen=True)
class Paragraph:
    """Lines wrapped to a width, set with their metrics and aligned in that width."""

    lines: Tuple[Line, ...]
    metrics: LineMetrics
    width: float
    align: Align = "l"

    @property
    def height(self) -> float:
        """Give the height of the lines, in points."""
        return len(self.lines) * self.metrics.pitch

    def draw(self, pdf: Pdf, x: float, y: float) -> None:
        """Draw the lines in the width from x, the first line's top at y."""
        baseline = y + self.metrics.baseline
        for line in self.lines:
            start = x + _share(self.width - line.width, self.align)
            for piece in line.pieces:
                _draw_piece(pdf, start + piece.x, baseline, piece)
            baseline += self.metrics.pitch


def _draw_piece(pdf: Pdf, x: float, baseline: float, piece: Piece) -> None:
    """Draw a piece from x on the baseline; a link is underlined, and its text and underline are what a click takes."""
    width = pdf.text(x, baseline, piece.text, piece.style)
    if piece.url:
        size = piece.style.size
        pdf.rule(x, x + width, baseline + size * 0.27 + 0.4, UNDERLINE)
        pdf.link(Box(x, baseline - size, width, size * 1.3), piece.url)


def paragraph(spans: Spans, style: Style, width: float, metrics: LineMetrics, align: Align = "l") -> Paragraph:
    """Wrap spans in style to width, to be set with metrics and aligned by align in that width."""
    return Paragraph(tuple(wrap(spans, style, width)), metrics, width, align)

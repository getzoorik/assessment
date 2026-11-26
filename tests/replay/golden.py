"""Goldens: what a scenario's run must give, kept as JSON text and compared as text."""

import difflib
import hashlib
import json
import os
import re
import zlib
from typing import Dict, List, Tuple

GOLDENS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "goldens")
# 1 rewrites the goldens from this run instead of comparing; python -m tests.replay update sets it.
UPDATE = "ZOORIK_UPDATE_GOLDENS"
# The report's engine writes each page's content as one Flate stream with this header.
STREAM = re.compile(rb"/Length (\d+) /Filter /FlateDecode >>\nstream\n")
TEXT_BLOCK = re.compile(rb"BT (.*?) ET", re.DOTALL)
# One run of text: its font, then its string in WinAnsi, with \, ( and ) escaped.
SHOWN = re.compile(rb"/F(\d) [\d.]+ Tf \(((?:\\.|[^\\)])*)\) Tj")
ESCAPED = re.compile(rb"\\(.)")
# The two characters the engine draws in the Symbol font (F3): the arrow and the minus sign.
SYMBOLS = {"\xae": "\u2192", "-": "\u2212"}


class GoldenMismatch(AssertionError):
    """A run's result is not its golden."""


class MalformedPdf(AssertionError):
    """A PDF that a reader can't open."""


def check(name: str, document: object, folder: str = GOLDENS) -> None:
    """Compare document with the golden called name, or write the golden when ZOORIK_UPDATE_GOLDENS is 1."""
    text = json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    path = os.path.join(folder, f"{name}.json")
    if os.environ.get(UPDATE) == "1":
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as golden:
            golden.write(text)
        return
    if not os.path.exists(path):
        raise GoldenMismatch(f"{name} has no golden yet; python -m tests.replay update writes it")
    with open(path, encoding="utf-8") as golden:
        expected = golden.read()
    if text != expected:
        diff = "".join(difflib.unified_diff(expected.splitlines(True), text.splitlines(True), "golden", "this run"))
        raise GoldenMismatch(f"{name} differs from its golden; python -m tests.replay update rewrites it\n{diff}")


def pdf_facts(pdf: bytes) -> Dict[str, object]:
    """Give what a golden keeps of a PDF, once its structure checks: its normalized hash, its pages and its text."""
    check_structure(pdf)
    pages = text(pdf)
    return {"sha256": hashlib.sha256(normalize(pdf)).hexdigest(), "pages": len(pages), "text": pages}


def streams(pdf: bytes) -> List[Tuple[int, int]]:
    """Give where each stream's data starts and its length, in file order; each page has one stream."""
    return [(match.end(), int(match.group(1))) for match in STREAM.finditer(pdf)]


def check_structure(pdf: bytes) -> None:
    """Check pdf is whole: its header and end, its cross-reference table, and the length and data of every stream."""
    if not pdf.startswith(b"%PDF-") or not pdf.rstrip().endswith(b"%%EOF"):
        raise MalformedPdf("the PDF header or its %%EOF is missing")
    _check_cross_references(pdf)
    found = streams(pdf)
    if not found:
        raise MalformedPdf("there is no page content")
    for start, length in found:
        if not pdf.startswith(b"\nendstream", start + length):
            raise MalformedPdf(f"the stream at byte {start} is not {length} bytes long")
        zlib.decompress(pdf[start : start + length])


def _check_cross_references(pdf: bytes) -> None:
    """Check that startxref points at the cross-reference table, and that the table gives each object's offset."""
    starts = re.findall(rb"startxref\s+(\d+)", pdf)
    if not starts:
        raise MalformedPdf("there is no startxref")
    start = int(starts[-1])
    table = re.match(rb"xref\s+0 (\d+)\s+", pdf[start:])
    if table is None:
        raise MalformedPdf("startxref does not point at a cross-reference table")
    size = int(table.group(1))
    entries = re.findall(rb"(\d{10}) \d{5} ([fn])", pdf[start + table.end() :])[:size]
    if len(entries) != size:
        raise MalformedPdf(f"the cross-reference table holds {len(entries)} of its {size} entries")
    for number, (offset, kind) in enumerate(entries):
        if kind == b"n" and not pdf.startswith(f"{number} 0 obj".encode(), int(offset)):
            raise MalformedPdf(f"object {number} is not at byte {int(offset)}")


def normalize(pdf: bytes) -> bytes:
    """Give pdf without what moves when any earlier byte changes: streams inflated, lengths and offsets dropped."""
    xref = int(re.findall(rb"startxref\s+(\d+)", pdf)[-1])
    pieces: List[bytes] = []
    position = 0
    for match in STREAM.finditer(pdf, 0, xref):
        start, length = match.end(), int(match.group(1))
        pieces.append(pdf[position : match.start()])
        pieces.append(b"/Filter /FlateDecode >>\nstream\n" + zlib.decompress(pdf[start : start + length]))
        position = start + length
    pieces.append(pdf[position:xref])
    trailer = pdf.index(b"trailer", xref)
    pieces.append(pdf[trailer : pdf.index(b"startxref", trailer)])
    return b"".join(pieces)


def text(pdf: bytes) -> List[List[str]]:
    """Give each page's lines of text in the order they were drawn: each BT ... ET block is one line."""
    pages = []
    for start, length in streams(pdf):
        content = zlib.decompress(pdf[start : start + length])
        pages.append(["".join(_shown(*run) for run in SHOWN.findall(block)) for block in TEXT_BLOCK.findall(content)])
    return pages


def _shown(font: bytes, string: bytes) -> str:
    """Give the text one Tj shows: WinAnsi in F1 and F2, and the arrow and the minus sign in F3."""
    data = ESCAPED.sub(rb"\1", string)
    if font == b"3":
        return "".join(SYMBOLS[char] for char in data.decode("latin-1"))
    return data.decode("cp1252")

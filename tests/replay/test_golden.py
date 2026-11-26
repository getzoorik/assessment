"""Goldens, and the facts they keep of a PDF."""

import os
import tempfile
import unittest
import zlib
from typing import List
from unittest import mock

from tests.replay.golden import UPDATE, GoldenMismatch, MalformedPdf, check, check_structure, normalize, pdf_facts

TITLE_PAGE = (
    b"BT 0 0 0 rg 0 Tc 1 0 0 1 56 780 Tm /F2 20 Tf (Cloud cost assessment) Tj ET\nq 1 0 0 rg 56 700 100 4 re f Q"
)
FIGURES_PAGE = (
    b"BT 0 0 0 rg 0 Tc 1 0 0 1 56 780 Tm /F1 10 Tf ($1,200 ) Tj 1 0 0 1 90 780 Tm 3 Tr 95 Tz /F3 10 Tf (\xae) Tj"
    b" 0 Tr 100 Tz 1 0 0 1 100 780 Tm /F1 10 Tf ( $800 a month \\(\x96 a third\\)) Tj ET\n"
    b"BT 0 0 0 rg 0 Tc 1 0 0 1 56 760 Tm /F1 10 Tf (Saving ) Tj 1 0 0 1 90 760 Tm 3 Tr /F3 10 Tf (-) Tj 0 Tr"
    b" 1 0 0 1 96 760 Tm /F1 10 Tf ($400) Tj ET"
)


def sample_pdf(pages: List[bytes], level: int) -> bytes:
    """Write pages of content as the report's engine writes a PDF, compressing each page at level."""
    kids = " ".join(f"{5 + 2 * index} 0 R" for index in range(len(pages)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode(),
        b"<< /Title <FEFF0043006C006F00750064> /Producer (Zoorik) >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for content in pages:
        data = zlib.compress(content, level)
        objects.append(f"<< /Type /Page /Parent 2 0 R /Contents {len(objects) + 2} 0 R >>".encode())
        objects.append(f"<< /Length {len(data)} /Filter /FlateDecode >>\nstream\n".encode() + data + b"\nendstream")
    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(pdf)
    pdf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    pdf += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    pdf += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(pdf)


class CheckTest(unittest.TestCase):
    """check, in a temporary goldens folder."""

    def setUp(self) -> None:
        """Make the temporary goldens folder."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name

    def write(self, name: str, document: object) -> None:
        """Write the golden as python -m tests.replay update would."""
        with mock.patch.dict(os.environ, {UPDATE: "1"}):
            check(name, document, self.folder)

    def test_update_writes_sorted_json_text_with_plain_line_ends(self) -> None:
        """A golden is JSON with sorted keys, UTF-8 and LF line ends, whatever the system."""
        self.write("sample", {"status": "Kubernetes 3/7 clusters · Disks 12/40 VMs", "exit": 0})
        with open(os.path.join(self.folder, "sample.json"), "rb") as golden:
            expected = '{\n "exit": 0,\n "status": "Kubernetes 3/7 clusters · Disks 12/40 VMs"\n}\n'
            self.assertEqual(golden.read(), expected.encode("utf-8"))

    def test_a_matching_run_passes(self) -> None:
        """A run that gives its golden passes."""
        self.write("sample", {"exit": 0})
        with mock.patch.dict(os.environ, {UPDATE: ""}):
            check("sample", {"exit": 0}, self.folder)

    def test_a_difference_fails_with_a_diff(self) -> None:
        """A run that differs from its golden fails, showing the difference."""
        self.write("sample", {"exit": 0})
        with mock.patch.dict(os.environ, {UPDATE: ""}), self.assertRaisesRegex(
            GoldenMismatch, '(?s)- "exit": 0.*\\+ "exit": 1'
        ):
            check("sample", {"exit": 1}, self.folder)

    def test_a_missing_golden_fails_with_the_way_to_write_it(self) -> None:
        """A run with no golden yet fails, saying how to write one."""
        with mock.patch.dict(os.environ, {UPDATE: ""}), self.assertRaisesRegex(GoldenMismatch, "update writes it"):
            check("sample", {"exit": 0}, self.folder)

    def test_a_golden_is_compared_as_text(self) -> None:
        """A golden checked out with Windows line ends still matches."""
        with open(os.path.join(self.folder, "sample.json"), "wb") as golden:
            golden.write(b'{\r\n "exit": 0\r\n}\r\n')
        with mock.patch.dict(os.environ, {UPDATE: ""}):
            check("sample", {"exit": 0}, self.folder)


class PdfFactsTest(unittest.TestCase):
    """check_structure, normalize and pdf_facts, on PDFs written as the report's engine writes them."""

    def test_the_facts_of_a_whole_pdf(self) -> None:
        """A whole PDF gives its page count and each page's lines, symbols and WinAnsi letters included."""
        facts = pdf_facts(sample_pdf([TITLE_PAGE, FIGURES_PAGE], 6))
        self.assertEqual(facts["pages"], 2)
        self.assertEqual(
            facts["text"],
            [
                ["Cloud cost assessment"],
                ["$1,200 \N{RIGHTWARDS ARROW} $800 a month (\N{EN DASH} a third)", "Saving \N{MINUS SIGN}$400"],
            ],
        )
        self.assertRegex(str(facts["sha256"]), "^[0-9a-f]{64}$")

    def test_the_normalized_pdf_ignores_compression_lengths_and_offsets(self) -> None:
        """The same pages compressed another way give other bytes but the same normalized PDF."""
        fast, small = sample_pdf([TITLE_PAGE, FIGURES_PAGE], 1), sample_pdf([TITLE_PAGE, FIGURES_PAGE], 9)
        self.assertNotEqual(fast, small)
        self.assertEqual(normalize(fast), normalize(small))
        self.assertNotEqual(normalize(fast), normalize(sample_pdf([FIGURES_PAGE, TITLE_PAGE], 1)))

    def test_a_damaged_pdf_fails_the_structure_check(self) -> None:
        """A PDF without its end, with a wrong offset or with a wrong stream length fails the check."""
        pdf = sample_pdf([TITLE_PAGE], 6)
        damaged = {
            "the PDF header or its %%EOF is missing": pdf[: -len(b"%%EOF\n")],
            "object 1 is not at byte 16": pdf.replace(b"0000000015 00000 n", b"0000000016 00000 n"),
            "is not [0-9]+ bytes long": pdf.replace(b"\nendstream", b"\nENDSTREAM", 1),
        }
        for reason, broken in damaged.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(MalformedPdf, reason):
                check_structure(broken)

"""Names: the package and the tests name the parts kubernetes and disks, and the products only where allowed."""

import os
import re
import unittest
from typing import Iterator, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SELF = "tests/unit/test_names.py"
# The old entry point keeps today's form until the package replaces it.
LEGACY = ("zoorik/__main__.py",)
# The report's closing page names the products, and the goldens hold the report's text.
PRODUCTS_ALLOWED = ("zoorik/report/closing.py", "tests/replay/goldens/")
PRODUCTS = re.compile(r"\b(coral|amoeba)\b", re.IGNORECASE)
NAMED = re.compile(r"coral|amoeba|k8s", re.IGNORECASE)
# k8s is allowed only inside the label keys k8s.io and k8s.aws.
K8S = re.compile(r"k8s(?!\.io\b|\.aws\b)", re.IGNORECASE)


def files() -> Iterator[Tuple[str, str]]:
    """Give each file under zoorik/ and tests/: its path from the repository root, with /, and its text."""
    for top in ("zoorik", "tests"):
        for folder, subfolders, names in os.walk(os.path.join(ROOT, top)):
            subfolders[:] = sorted(name for name in subfolders if name != "__pycache__" and not name.startswith("."))
            for name in sorted(names):
                path = os.path.join(folder, name)
                relative = os.path.relpath(path, ROOT).replace(os.sep, "/")
                if not name.startswith(".") and relative not in LEGACY:
                    with open(path, encoding="utf-8") as text:
                        yield relative, text.read()


def lines_matching(pattern: "re.Pattern[str]", skip: Tuple[str, ...]) -> List[str]:
    """Name each line, as path:number, where pattern matches, in the files not under skip."""
    return [
        f"{path}:{number}"
        for path, text in files()
        if not path.startswith(skip)
        for number, line in enumerate(text.splitlines(), 1)
        if pattern.search(line)
    ]


class NamesTest(unittest.TestCase):
    """File names and the full text of every file under zoorik/ and tests/."""

    def test_no_file_or_folder_is_named_for_a_product_or_k8s(self) -> None:
        """No file or folder name holds coral, amoeba or k8s."""
        self.assertEqual([path for path, _ in files() if NAMED.search(path)], [])

    def test_the_products_are_named_only_on_the_closing_page(self) -> None:
        """Identifiers, strings, comments and docstrings name the products only on the closing page and in goldens."""
        self.assertEqual(lines_matching(PRODUCTS, (SELF, *PRODUCTS_ALLOWED)), [])

    def test_k8s_appears_only_in_label_keys(self) -> None:
        """k8s appears only as part of k8s.io and k8s.aws."""
        self.assertEqual(lines_matching(K8S, (SELF,)), [])

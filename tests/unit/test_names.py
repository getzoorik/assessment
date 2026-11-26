"""Names: the whole tree names the parts kubernetes and disks, and the products only where it says what to do next."""

import os
import re
import unittest
from typing import Iterator, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SELF = "tests/unit/test_names.py"
# .github is the tree's one hidden folder; every other one, such as .git, belongs to a tool.
HIDDEN_KEPT = ".github"
# Folders that hold builds, caches or installed packages, none of them the project's own files.
SKIPPED = frozenset({"__pycache__", "build", "dist", "node_modules"})
# A file with a NUL byte in its first 8,000 bytes is binary, such as the README's banner, as git counts it.
BINARY_PROBE = 8000
# The report's closing page names the products, and the goldens hold the report's text.
PRODUCTS_ALLOWED = ("zoorik/report/closing.py", "tests/replay/goldens/")
README = "README.md"
# The README names the products only under this heading, as the report's Next steps does, and in its trademark line.
NEXT_STEPS = "## Next steps"
TRADEMARK = "names and logos belong to Zoorik Private Limited"
PRODUCTS = re.compile(r"\b(coral|amoeba)\b", re.IGNORECASE)
NAMED = re.compile(r"coral|amoeba|k8s", re.IGNORECASE)
# k8s is allowed only inside the label keys k8s.io and k8s.aws.
K8S = re.compile(r"k8s(?!\.io\b|\.aws\b)", re.IGNORECASE)


def kept(folder: str) -> bool:
    """Tell whether a folder holds the project's own files."""
    if folder.startswith("."):
        return folder == HIDDEN_KEPT
    return folder not in SKIPPED and not folder.endswith(".egg-info")


def paths() -> Iterator[str]:
    """Give the path of each file of the tree from the repository root, with /, in a fixed order."""
    for folder, subfolders, names in os.walk(ROOT):
        subfolders[:] = sorted(name for name in subfolders if kept(name))
        for name in sorted(names):
            yield os.path.relpath(os.path.join(folder, name), ROOT).replace(os.sep, "/")


def files() -> Iterator[Tuple[str, str]]:
    """Give each text file of the tree: its path and its text."""
    for path in paths():
        with open(os.path.join(ROOT, path), "rb") as source:
            data = source.read()
        if b"\0" not in data[:BINARY_PROBE]:
            yield path, data.decode("utf-8")


def lines_matching(pattern: "re.Pattern[str]", skip: Tuple[str, ...]) -> List[str]:
    """Name each line, as path:number, where pattern matches, in the files not under skip."""
    return [
        f"{path}:{number}"
        for path, text in files()
        if not path.startswith(skip)
        for number, line in enumerate(text.splitlines(), 1)
        if pattern.search(line)
    ]


def outside_next_steps(text: str) -> List[int]:
    """Give the numbers of the lines of a README that name a product outside its Next steps and its trademark line."""
    found: List[int] = []
    section = ""
    for number, line in enumerate(text.splitlines(), 1):
        if line.startswith("## "):
            section = line
        if PRODUCTS.search(line) and section != NEXT_STEPS and TRADEMARK not in line:
            found.append(number)
    return found


class NamesTest(unittest.TestCase):
    """File names and the full text of every text file of the tree."""

    def test_no_file_or_folder_is_named_for_a_product_or_k8s(self) -> None:
        """No file or folder name holds coral, amoeba or k8s."""
        self.assertEqual([path for path in paths() if NAMED.search(path)], [])

    def test_the_products_are_named_only_where_the_reader_is_told_what_next(self) -> None:
        """Code, docs and settings name the products only on the closing page, in the goldens and in the README."""
        self.assertEqual(lines_matching(PRODUCTS, (SELF, README, *PRODUCTS_ALLOWED)), [])

    def test_the_readme_names_the_products_only_in_next_steps_and_its_trademark_line(self) -> None:
        """The README names the products under its Next steps heading and in its trademark line, nowhere else."""
        with open(os.path.join(ROOT, README), encoding="utf-8") as readme:
            self.assertEqual(outside_next_steps(readme.read()), [])

    def test_k8s_appears_only_in_label_keys(self) -> None:
        """k8s appears only as part of k8s.io and k8s.aws."""
        self.assertEqual(lines_matching(K8S, (SELF,)), [])


class DetectorsTest(unittest.TestCase):
    """Each detector finds what it looks for, so no rule passes by finding nothing."""

    def test_the_tree_is_read_from_its_root(self) -> None:
        """The scan reaches the README, the package, the tests, the workflows and the launcher."""
        found = set(paths())
        for path in (README, "zoorik/cli.py", SELF, ".github/workflows/ci.yaml", "npm/bin/zoorik.js"):
            self.assertIn(path, found)

    def test_a_product_outside_next_steps_is_found(self) -> None:
        """A product named under another heading is found; under Next steps and in the trademark line it is not."""
        text = f"## What it reads\nCoral\n{NEXT_STEPS}\nCoral and Amoeba\n## Contributing\nAmoeba\n{TRADEMARK} Coral"
        self.assertEqual(outside_next_steps(text), [2, 6])

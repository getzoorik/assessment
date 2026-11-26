"""The layer rules import-linter can't express, read from the package's source with ast."""

import ast
import os
import unittest
from typing import Callable, Iterator, List, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The old entry point keeps today's form until the package replaces it.
LEGACY = ("zoorik/__main__.py",)
CATCH_ALL = ("Exception", "BaseException")


def sources() -> Iterator[Tuple[str, ast.Module]]:
    """Give each module of the package: its path from the repository root, with /, and its syntax tree."""
    for folder, subfolders, names in os.walk(os.path.join(ROOT, "zoorik")):
        subfolders[:] = sorted(name for name in subfolders if name != "__pycache__")
        for name in sorted(names):
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if name.endswith(".py") and relative not in LEGACY:
                with open(path, encoding="utf-8") as source:
                    yield relative, ast.parse(source.read(), relative)


def imports(tree: ast.AST, module: str) -> bool:
    """Tell whether tree imports module or a module inside it, in any form of import."""
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return any(name == module or name.startswith(module + ".") for name in names)


def argv_heads(tree: ast.AST) -> Set[str]:
    """Name the strings that list and tuple literals in tree start with, as an argv starts with its program."""
    heads: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                heads.add(first.value)
    return heads


def catches_everything(handler: ast.ExceptHandler) -> bool:
    """Tell whether an except clause catches everything: a bare except, or except Exception or BaseException."""
    if handler.type is None:
        return True
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(kind, ast.Name) and kind.id in CATCH_ALL for kind in caught)


def catch_alls(tree: ast.AST) -> int:
    """Count the except clauses in tree that catch everything."""
    return sum(1 for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler) and catches_everything(node))


def starts_programs(tree: ast.AST) -> bool:
    """Tell whether tree imports subprocess."""
    return imports(tree, "subprocess")


def makes_http_calls(tree: ast.AST) -> bool:
    """Tell whether tree imports urllib.request or http.client."""
    return imports(tree, "urllib.request") or imports(tree, "http.client")


def builds_cloud_commands(tree: ast.AST) -> bool:
    """Tell whether tree holds an argv that starts with az or aws."""
    return bool(argv_heads(tree) & {"az", "aws"})


def builds_kubectl_commands(tree: ast.AST) -> bool:
    """Tell whether tree holds an argv that starts with kubectl or kubelogin."""
    return bool(argv_heads(tree) & {"kubectl", "kubelogin"})


def has_catch_all(tree: ast.AST) -> bool:
    """Tell whether tree catches everything anywhere."""
    return catch_alls(tree) > 0


def outside(rule: Callable[[ast.AST], bool], allowed: Tuple[str, ...]) -> List[str]:
    """Name the modules where rule finds something, other than the allowed modules and folders."""
    return [path for path, tree in sources() if rule(tree) and not path.startswith(allowed)]


class LayerRulesTest(unittest.TestCase):
    """Each rule holds for every module of the package."""

    def test_only_the_runner_starts_programs(self) -> None:
        """Only zoorik/system/process.py imports subprocess."""
        self.assertEqual(outside(starts_programs, ("zoorik/system/process.py",)), [])

    def test_only_the_http_policy_makes_http_calls(self) -> None:
        """Only zoorik/pricing/http.py imports urllib.request or http.client."""
        self.assertEqual(outside(makes_http_calls, ("zoorik/pricing/http.py",)), [])

    def test_only_cloud_builds_az_and_aws_commands(self) -> None:
        """An argv that starts with az or aws appears only under zoorik/cloud/."""
        self.assertEqual(outside(builds_cloud_commands, ("zoorik/cloud/",)), [])

    def test_only_kubectl_builds_kubectl_and_kubelogin_commands(self) -> None:
        """An argv that starts with kubectl or kubelogin appears only under zoorik/kubectl/."""
        self.assertEqual(outside(builds_kubectl_commands, ("zoorik/kubectl/",)), [])

    def test_one_catch_all_in_cli_and_none_elsewhere(self) -> None:
        """No module catches everything, except cli.py with its one handler for unexpected errors."""
        self.assertEqual(outside(has_catch_all, ("zoorik/cli.py",)), [])
        self.assertLessEqual(sum(catch_alls(tree) for path, tree in sources() if path == "zoorik/cli.py"), 1)


class DetectorsTest(unittest.TestCase):
    """Each detector finds what it looks for, so no rule passes by finding nothing."""

    def test_every_form_of_import_is_seen(self) -> None:
        """Plain, dotted, aliased and from-imports all count, and a sibling module doesn't."""
        for source in ("import subprocess", "import subprocess as sp", "from subprocess import run"):
            self.assertTrue(imports(ast.parse(source), "subprocess"), source)
        for source in ("import urllib.request", "from urllib import request", "from urllib.request import urlopen"):
            self.assertTrue(imports(ast.parse(source), "urllib.request"), source)
        self.assertTrue(imports(ast.parse("from http import client"), "http.client"))
        self.assertFalse(imports(ast.parse("from urllib.parse import urlencode"), "urllib.request"))

    def test_argv_literals_are_seen(self) -> None:
        """Lists and tuples that start with a string are seen, wherever they are."""
        tree = ast.parse('run(["az", "account", "show"])\nARGV = ("kubectl", "top", "pods")\nn = [1, 2]')
        self.assertEqual(argv_heads(tree), {"az", "kubectl"})

    def test_each_rule_finds_the_modules_it_allows(self) -> None:
        """The runner, the HTTP policy, the cloud readers and the direct cluster read are found, so each rule checks."""
        found = {path: tree for path, tree in sources()}
        self.assertEqual([path for path, tree in found.items() if starts_programs(tree)], ["zoorik/system/process.py"])
        self.assertEqual([path for path, tree in found.items() if makes_http_calls(tree)], ["zoorik/pricing/http.py"])
        self.assertEqual(
            [path for path, tree in found.items() if builds_cloud_commands(tree)],
            ["zoorik/cloud/aws.py", "zoorik/cloud/azure.py"],
        )
        self.assertEqual(
            [path for path, tree in found.items() if builds_kubectl_commands(tree)], ["zoorik/kubectl/direct.py"]
        )

    def test_catch_alls_are_counted(self) -> None:
        """A bare except, except Exception and except BaseException count, alone or in a tuple."""
        handlers = ["", " Exception", " BaseException", " (KeyError, Exception)", " KeyError", " (KeyError, OSError)"]
        source = "".join(f"try:\n    pass\nexcept{caught}:\n    pass\n" for caught in handlers)
        self.assertEqual(catch_alls(ast.parse(source)), 4)

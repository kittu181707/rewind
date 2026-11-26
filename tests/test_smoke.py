"""Smoke tests verifying package layout and import boundaries."""

from pathlib import Path
import ast

import rewind
from rewind import cli


def test_package_imports():
    assert rewind.__version__ == "0.1.0"
    parser = cli.build_parser()
    args = parser.parse_args(["check", "--config", "rewind.config.yaml"])
    assert args.command == "check"
    assert args.config == "rewind.config.yaml"


def test_rewind_never_imports_examples():
    rewind_dir = Path(__file__).resolve().parent.parent / "rewind"
    for py_file in rewind_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("examples"), f"{py_file} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith("examples"), f"{py_file} imports {mod}"

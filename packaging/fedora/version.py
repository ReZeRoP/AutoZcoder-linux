#!/usr/bin/env python3
"""Read release metadata without importing the application or its dependencies."""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read_version(root: Path = ROOT) -> str:
    tree = ast.parse((root / "zcode_gateway/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        ):
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version):
                return version
    raise ValueError("Expected a literal MAJOR.MINOR.PATCH __version__")


def stage_path(root: Path = ROOT) -> Path:
    return root / "build/fedora" / f"autozcoder-{read_version(root)}-fedora"


if __name__ == "__main__":
    print(read_version())

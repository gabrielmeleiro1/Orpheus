"""Enforce the web-layer import boundary.

`modules.web` is a thin interface layer: it may import `modules.app` and
`modules.contracts` but must never reach into `modules.pipeline` or
`modules.tts` (see docs/architecture/python-boundaries.md).
"""

from __future__ import annotations

import ast
from pathlib import Path

_FORBIDDEN_PREFIXES = ("modules.pipeline", "modules.tts")
_WEB_DIR = Path(__file__).parent.parent / "modules" / "web"


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_web_layer_does_not_import_pipeline_or_tts() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(_WEB_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bad = sorted(
            module
            for module in _imported_modules(tree)
            if any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in _FORBIDDEN_PREFIXES
            )
        )
        if bad:
            offenders[str(path.relative_to(_WEB_DIR.parent.parent))] = bad

    assert offenders == {}

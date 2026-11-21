"""The source gate must retain failed command diagnostics on Windows consoles."""

from __future__ import annotations

import io
import json
import sys

from scripts import check_desktop


def test_failed_command_retains_unicode_diagnostics_on_legacy_console(tmp_path, monkeypatch):
    output = tmp_path / "validation"
    raw_console = io.BytesIO()
    console = io.TextIOWrapper(raw_console, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(check_desktop, "commands", lambda: {"lint": [(tmp_path, [
        sys.executable, "-c", "import sys; print('Failed at \\u276f test.py:1'); sys.exit(3)",
    ])]})

    assert check_desktop.main(["--only", "lint", "--output-dir", str(output)]) == 3
    console.flush()
    assert "Failed at \u276f test.py:1" in (output / "lint-1.log").read_text(encoding="utf-8")
    assert b"test.py:1" in raw_console.getvalue()
    assert json.loads((output / "results.json").read_text(encoding="utf-8"))[0]["exit_code"] == 3


def test_source_gates_run_the_same_commands_as_ci():
    """The pre-push hook delegates here, so drift from CI would pass bad pushes."""
    workflow = (check_desktop.REPO_ROOT / ".github/workflows/source-validation.yml").read_text(
        encoding="utf-8"
    )
    plan = check_desktop.commands()
    for stage in ("lint", "python", "frontend", "rust"):
        for _cwd, command in plan[stage]:
            assert " ".join(command[1:]) in workflow, command

#!/usr/bin/env python3
"""Run repeatable desktop source gates on Mac or Windows, retaining each log."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
STAGES = ("lint", "python", "frontend", "rust", "smoke")


def commands() -> dict[str, list[tuple[Path, list[str]]]]:
    desktop = REPO_ROOT / "desktop"
    rust = desktop / "src-tauri"
    npm = shutil.which("npm") or "npm"
    cargo = shutil.which("cargo") or "cargo"
    return {
        "lint": [(REPO_ROOT, [sys.executable, "-m", "ruff", "check", "."])],
        "python": [(REPO_ROOT, [sys.executable, "run_tests.py", "--contract"])],
        "frontend": [
            (desktop, [npm, "run", "typecheck"]),
            (desktop, [npm, "test", "--", "--run"]),
            (desktop, [npm, "run", "build"]),
        ],
        "rust": [
            (rust, [cargo, "test", "--locked", "--", "--test-threads=1"]),
            (rust, [cargo, "check", "--locked"]),
        ],
        "smoke": [(REPO_ROOT, [
            sys.executable, "scripts/desktop_runtime_smoke.py", "--backend", "python",
            "--check-ffmpeg-runtime",
        ])],
    }


def main(argv: list[str] | None = None) -> int:
    # Keep the original command's failure visible on legacy Windows consoles.
    # Full Unicode diagnostics are retained in the UTF-8 log regardless.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", choices=STAGES, default=list(STAGES))
    parser.add_argument("--output-dir", type=Path, help="New directory for command logs and results.json.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running or writing files.")
    args = parser.parse_args(argv)
    plan = commands()
    if args.dry_run:
        for stage in args.only:
            for cwd, command in plan[stage]:
                print(json.dumps({"stage": stage, "cwd": str(cwd), "command": command}))
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = args.output_dir or REPO_ROOT / ".artifacts" / "validation" / f"{stamp}-{uuid4().hex[:8]}"
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ)
    # Rust tests launch Python by name. Use this same environment consistently.
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    results = []
    print(f"Validation logs: {output}", flush=True)
    for stage in args.only:
        for index, (cwd, command) in enumerate(plan[stage], 1):
            log = output / f"{stage}-{index}.log"
            print(f"Running {stage} ({index}/{len(plan[stage])})", flush=True)
            with log.open("w", encoding="utf-8") as stream:
                try:
                    code = subprocess.run(command, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT).returncode
                except OSError as error:
                    stream.write(f"Could not run command: {error}\n")
                    code = 127
            results.append({"stage": stage, "command": command, "cwd": str(cwd), "exit_code": code, "log": str(log)})
            (output / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
            print(f"{'PASS' if code == 0 else 'FAIL'} {stage}: {log}", flush=True)
            if code:
                print("\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]))
                return code if code > 0 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

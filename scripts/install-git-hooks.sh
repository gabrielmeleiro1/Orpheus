#!/usr/bin/env bash
# Point Git at the tracked Orpheus hooks so every worktree runs the current copy.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# A relative hooksPath resolves against each worktree's root when a hook runs.
git config core.hooksPath scripts/hooks

cat <<EOF
Installed Orpheus pre-push validation (core.hooksPath = scripts/hooks).

Before every:
  git push

this repository will run:
  - npm ci
  - python scripts/check_desktop.py --only lint python frontend rust
    (Ruff, run_tests.py --contract, the frontend type check, Vitest and
    build, and the locked Tauri tests and check, as in CI)

The push is blocked if any check fails.
EOF

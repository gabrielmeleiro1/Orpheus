#!/usr/bin/env bash
# Install the Orpheus pre-push validation hook for this clone.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

chmod +x \
  "$ROOT/scripts/hooks/pre-push" \
  "$ROOT/scripts/install-git-hooks.sh"

mkdir -p "$ROOT/.git/hooks"
cp "$ROOT/scripts/hooks/pre-push" "$ROOT/.git/hooks/pre-push"
chmod +x "$ROOT/.git/hooks/pre-push"

cat <<EOF
Installed Orpheus pre-push validation for this repository clone.

Before every:
  git push

this clone will run:
  - python -m ruff check .
  - python -m pytest tests -m "not property and not benchmark and not slow and not ui"
  - npm ci
  - npm run typecheck
  - npm test -- --run
  - cargo test
  - cargo check

The push is blocked if any check fails.
EOF

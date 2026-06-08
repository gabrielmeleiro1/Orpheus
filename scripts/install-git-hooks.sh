#!/usr/bin/env bash
# Install the Orpheus post-push test workflow for this clone.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

chmod +x \
  "$ROOT/scripts/git-push.sh" \
  "$ROOT/scripts/hooks/run-post-push-tests.sh" \
  "$ROOT/scripts/install-git-hooks.sh"

# Git has no native post-push hook, so wrap `git push` locally.
git config --local alias.push \
  "!f() { bash \"$ROOT/scripts/git-push.sh\" \"\$@\"; }; f"

cat <<EOF
Installed Orpheus post-push tests for this repository clone.

After every successful:
  git push

this clone will run:
  - python -m ruff check .
  - python run_tests.py --contract
  - desktop typecheck + cargo test + cargo check

Skip desktop checks:
  ORPHEUS_POST_PUSH_SKIP_DESKTOP=1 git push

Bypass the wrapper and push without local tests:
  git -c alias.push= push

Remote CI still runs via .github/workflows/post-commit.yml on every push.
EOF

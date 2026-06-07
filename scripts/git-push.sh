#!/usr/bin/env bash
# Push to the remote, then run the post-push test suite on success.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if ! git -c alias.push= push "$@"; then
  exit $?
fi

exec "$ROOT/scripts/hooks/run-post-push-tests.sh"

#!/usr/bin/env bash
# Sync the Orpheus source tree to the Windows desktop benchmark host over SSH.
# The desktop copy lives directly in %USERPROFILE% and is not a git checkout,
# so this pushes a tarball of the tracked source dirs and extracts it there.
set -euo pipefail

HOST="${ORPHEUS_DESKTOP_HOST:-desktop}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SYNC_PATHS=(
  modules
  orpheus
  scripts
  benchmarks
  apps
  tools
  tests
  pytest.ini
  ruff.toml
  run_tests.py
  requirements.txt
  requirements-dev.txt
)

cd "$REPO_ROOT"
# COPYFILE_DISABLE stops macOS bsdtar from emitting ._* AppleDouble files.
COPYFILE_DISABLE=1 tar czf - \
  --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  "${SYNC_PATHS[@]}" |
  ssh "$HOST" 'tar xzf - -C %USERPROFILE%\Developer\Orpheus-main'
echo "synced ${#SYNC_PATHS[@]} paths to $HOST:Developer\\Orpheus-main"

#!/usr/bin/env bash
# Run the same local checks as CI after a successful git push.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$ROOT"

echo ""
echo "============================================================"
echo "   ORPHEUS - POST-PUSH TESTS"
echo "============================================================"
echo ""

echo "▸ Ruff"
python -m ruff check .

echo ""
echo "▸ Python contract"
python run_tests.py --contract

if [[ "${ORPHEUS_POST_PUSH_SKIP_DESKTOP:-0}" == "1" ]]; then
  echo ""
  echo "Skipped desktop checks (ORPHEUS_POST_PUSH_SKIP_DESKTOP=1)."
else
  echo ""
  echo "▸ Desktop typecheck"
  (
    cd desktop
    npm run typecheck
  )

  echo ""
  echo "▸ Tauri tests"
  (
    cd desktop/src-tauri
    cargo test -- --skip build_dev_launch_config_matches_desktop_runtime_contract
  )

  echo ""
  echo "▸ Tauri check"
  (
    cd desktop/src-tauri
    cargo check
  )
fi

echo ""
echo "POST-PUSH TESTS PASSED"
echo ""

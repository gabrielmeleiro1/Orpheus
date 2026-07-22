# Contributing

Thanks for your interest in Orpheus.

## Getting Set Up

Follow the Quick Start in [README.md](README.md), then read
[docs/README.md](docs/README.md) for the documentation reading order.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#where-to-change-x) maps common
changes to the files that own them.

## Before Opening a PR

1. Follow the branch, commit-message, and staging conventions in
   [docs/COMMIT_WORKFLOW.md](docs/COMMIT_WORKFLOW.md).
2. Run the local gates described in [docs/TESTING.md](docs/TESTING.md):

   ```bash
   python -m ruff check .
   python run_tests.py --contract
   cd desktop && npm run typecheck && npm test -- --run
   ```

3. Open the PR against `main`. CI runs the same gates on macOS and Windows and
   must pass.

## Reporting Issues

Use GitHub issues for bugs and feature requests. For security problems, follow
[SECURITY.md](SECURITY.md) instead of opening a public issue.

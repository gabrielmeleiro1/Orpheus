# Contributing

Thanks for your interest in Orpheus. Bug reports, fixes, and focused
improvements are welcome.

## Set up

1. Follow the source install in the [README](README.md#from-source).
2. Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
   [Where to change X](docs/ARCHITECTURE.md#where-to-change-x) maps common
   changes to the files that own them.
3. Optionally install the pre-push hook, which runs the local gates before
   every `git push`:

   ```bash
   bash scripts/install-git-hooks.sh
   ```

## Branches

Branch from the latest `main` and use a short, descriptive name with a type
prefix:

```bash
git switch main
git pull
git switch -c fix/source-preflight
```

Common prefixes are `feature/`, `fix/`, `refactor/`, `docs/`, and `ci/`.

## Commits

Use `type: short summary`, for example `fix: handle missing source files`.

| Type | Use for |
| --- | --- |
| `feat` | New behavior |
| `fix` | Bug fixes |
| `refactor` | Code changes with no intended behavior change |
| `perf` | Performance work |
| `test` | Tests only |
| `docs` | Documentation only |
| `chore` | Tooling, configuration, dependencies, packaging |
| `ci` | GitHub Actions |

A scope is optional, as in `test(desktop): cover model setup`. Keep commits
small and messages specific; avoid `updates` or `misc`. If you skip a check,
say why in the commit body.

Stage explicit paths and review the result before committing:

```bash
git status --short
git add modules/storage/ tests/test_storage_repository.py
git diff --cached
```

Never commit local state or build output: `.venv/`, `.env.local`, `data/*.db`,
`output/`, `temp/`, `.artifacts/`, `desktop/node_modules/`, `desktop/dist/`,
`desktop/src-tauri/binaries/`, or `desktop/src-tauri/target/`. `.gitignore`
covers these.

## Checks

Run the checks that match your change. For most changes:

```bash
python -m ruff check .
python run_tests.py --contract
cd desktop && npm run typecheck && npm test -- --run
```

For Rust or Tauri changes, also run
`cd desktop/src-tauri && cargo test && cargo check`. To run every source gate
with retained logs, use `python scripts/check_desktop.py`.
[docs/TESTING.md](docs/TESTING.md) lists focused checks for each subsystem.

## Pull requests

Open pull requests against `main`. The `Source validation` workflow runs lint,
the Python gate, frontend tests, and Rust checks on macOS and Windows; it must
pass before merge. Update the docs when you change behavior, commands, or
architecture boundaries.

## Reporting issues

Use GitHub issues for bugs and feature requests. Include your OS, how you
installed Orpheus, and `data/logs/backend.log` from the app data directory
(see [data locations](docs/RUN_APPLICATION.md#data-locations)). For security
problems, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

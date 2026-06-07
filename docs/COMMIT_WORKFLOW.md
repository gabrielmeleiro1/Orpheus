# Commit Workflow

Keep Git history simple. Work on a branch, make small commits, review what is
staged, and run the checks that match the change.

## Branches

Start from the latest `main`:

```bash
git switch main
git pull
```

Create a branch with a short, descriptive name:

```bash
git switch -c feature/library-filters
git switch -c fix/source-preflight
git switch -c docs/testing-notes
```

Use prefixes when helpful:

- `feature/`
- `fix/`
- `refactor/`
- `docs/`
- `ci/`

## Commit Messages

Use this format:

```text
type: short summary
```

Common types:

- `feat`: feature
- `fix`: bug fix
- `refactor`: code change without intended behavior change
- `test`: tests
- `docs`: documentation
- `chore`: tooling, config, dependencies, packaging
- `ci`: GitHub Actions or other CI changes
- `perf`: performance work

Examples:

```text
feat: add library filters
fix: handle missing source files
refactor: split conversion runtime options
test: add storage migration coverage
docs: update setup instructions
ci: add validation workflow
```

Keep the message short and specific. Avoid vague messages like `updates`,
`cleanup`, or `misc`.

## Staging

Check the working tree:

```bash
git status --short --ignored
```

Stage explicit paths:

```bash
git add README.md docs/
git add modules/ tests/test_storage_repository.py
git add desktop/src desktop/src-tauri/src
```

Review what will be committed:

```bash
git diff --cached --stat
git diff --cached
```

Do not commit local state or generated files:

- `data/*.db`
- `tests/data/*.db`
- `output/`
- `temp/`
- `desktop/node_modules/`
- `desktop/dist/`
- `desktop/src-tauri/binaries/`
- `desktop/src-tauri/target/`
- caches, bytecode, coverage files, and OS metadata

## Checks

Run checks that match the change. For broad Python changes:

```bash
python run_tests.py --contract
python -m ruff check .
```

For frontend changes:

```bash
cd desktop
npm ci
npm run typecheck
```

For Rust/Tauri changes:

```bash
cd desktop/src-tauri
cargo test
cargo check
```

If a check is skipped, note why in the commit body.

## Push

Push the branch:

```bash
git push -u origin feature/library-filters
```

Open a pull request when the branch is ready for review.

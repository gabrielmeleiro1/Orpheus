# Commit Workflow

Use small commits that are easy to review and easy to undo. Stage files
explicitly and check the diff before committing.

## First Commit Order

Use this order for the initial repository history:

1. `chore: add project metadata`
   - `LICENSE`
   - `.gitignore`
   - `.env.example`
   - package manifests and lockfiles
   - test and lint config

2. `docs: add project documentation`
   - `README.md`
   - `docs/`
   - `data/README.md`

3. `feat: add core backend`
   - `modules/`
   - `apps/`

4. `feat: add desktop app`
   - `desktop/src/`
   - `desktop/src-tauri/src/`
   - committed app icons and Tauri config

5. `test: add test suite`
   - `tests/`
   - `run_tests.py`

6. `chore: add scripts and benchmarks`
   - `scripts/`
   - `tools/`
   - `benchmarks/`
   - `vulture_whitelist.py`

7. `ci: add validation workflow`
   - `.github/workflows/`

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
docs: add setup instructions
feat: add conversion pipeline
fix: handle missing source files
test: add storage migration tests
ci: add validation workflow
```

Keep subjects short and specific. Avoid vague messages like `updates`,
`cleanup`, or `misc`.

## Before Committing

Check status:

```bash
git status --short --ignored
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

Stage explicit paths:

```bash
git add README.md docs/
git add modules/ apps/
git add desktop/src desktop/src-tauri/src
```

Review the staged diff:

```bash
git diff --cached --stat
git diff --cached
```

Run checks that match the change. For broad changes:

```bash
python run_tests.py --contract
python -m ruff check .
cd desktop && npm ci && npm run typecheck
```

When touching Rust/Tauri code:

```bash
cd desktop/src-tauri
cargo test
cargo check
```

If a check is skipped, note why in the commit body.

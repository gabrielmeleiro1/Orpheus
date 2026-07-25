# Source-Only Cross-Platform Validation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make a fresh Orpheus source checkout clearly installable and testable on macOS and Windows without adding release production.

**Architecture:** Treat documentation and repository configuration as an executable source-checkout contract. A focused pytest module checks the Windows commands, the GitHub Actions matrix and its release-free boundary, and ignored local outputs; the implementation remains limited to Markdown, one workflow, and `.gitignore`.

**Tech Stack:** Markdown, PowerShell documentation, GitHub Actions YAML, pytest, Git.

---

### Task 1: Windows source setup

**Files:**
- Create: `tests/test_source_checkout_contract.py`
- Modify: `README.md`
- Modify: `docs/DEPENDENCIES.md`
- Modify: `docs/RUN_APPLICATION.md`

1. Add a test that requires PowerShell virtual-environment creation and activation, manifest installation, the base-profile dry run, the contract suite, and desktop development commands.
2. Run `python -m pytest --no-cov tests/test_source_checkout_contract.py -v` and confirm the documentation contract fails.
3. Add prerequisite and source-checkout instructions for Windows, with CUDA and model downloads explicitly optional.
4. Run the focused test and confirm it passes.
5. Commit as `docs: add Windows source setup`.

### Task 2: Test-only macOS and Windows CI

**Files:**
- Modify: `tests/test_source_checkout_contract.py`
- Create: `.github/workflows/source-validation.yml`
- Modify: `docs/TESTING.md`

1. Add a test requiring macOS and Windows matrix entries, source checks, and the absence of packaging, signing, releases, and artifact uploads.
2. Run the focused test and confirm the workflow contract fails.
3. Add the least-privilege workflow and document exactly what it verifies and excludes.
4. Run the focused test and confirm it passes.
5. Commit as `ci: verify macOS and Windows source checkouts`.

### Task 3: Common local-output ignores

**Files:**
- Modify: `tests/test_source_checkout_contract.py`
- Modify: `.gitignore`

1. Add a test using `git check-ignore` for common Python packaging and test-environment outputs.
2. Run the focused test and confirm the ignore contract fails.
3. Add narrow ignore rules for those local generated paths.
4. Run the focused test and confirm it passes.
5. Commit as `chore: ignore common local build outputs`.

### Task 4: Integrated verification

**Files:**
- Verify only.

1. Run `python -m pytest --no-cov tests/test_source_checkout_contract.py -v`.
2. Run `scripts/hooks/pre-push`.
3. Inspect `git status --short`, the final commit subjects, and commit bodies for accidental files or attribution trailers.
4. Record Windows CI as configured but not executed locally until GitHub runs it.

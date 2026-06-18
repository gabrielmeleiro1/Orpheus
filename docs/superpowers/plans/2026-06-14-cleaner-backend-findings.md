# Cleaner Backend Findings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the review findings around cleaner backend metadata tests, hardcoded backend choices, facade burn-down coverage, and registry fixture reuse.

**Architecture:** Add focused tests around the existing helper and CLI surfaces before changing behavior. Keep production changes minimal: make desktop CUDA fields metadata-driven, derive benchmark choices from the cleaner registry/config, and reuse existing registry isolation fixtures.

**Tech Stack:** TypeScript, Vitest, Python, pytest, argparse.

---

### Task 1: Desktop Cleaner Metadata Tests

**Files:**
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Create: `desktop/src/features/conversion/cleanerBackendHelpers.test.ts`
- Modify: `desktop/src/features/conversion/cleanerBackendHelpers.ts`

- [ ] **Step 1: Add a desktop test runner and failing helper tests**

Add Vitest to the desktop package and a `test` script. Create helper tests that cover:
- local backend availability is gated by `cleaner_runtime_availability`
- unknown local availability is treated as available for selection
- CUDA runtime payload fields are emitted only when metadata advertises device or quantization support
- CUDA defaults come from metadata
- runtime status reports ready, unavailable with probe detail, checking, API-ready, and missing-backend states

- [ ] **Step 2: Run desktop tests to verify the metadata-only CUDA test fails**

Run: `cd desktop && npm test -- --run src/features/conversion/cleanerBackendHelpers.test.ts`

Expected before implementation: failure for a `cuda` option without CUDA metadata because `cleanerBackendUsesCudaFields` still checks `option.id === "cuda"`.

- [ ] **Step 3: Remove the hardcoded CUDA branch**

Change `cleanerBackendUsesCudaFields` so it returns true only when `default_device` is set or `supported_quantizations` is non-empty.

- [ ] **Step 4: Re-run desktop tests and typecheck**

Run:
- `cd desktop && npm test -- --run src/features/conversion/cleanerBackendHelpers.test.ts`
- `cd desktop && npm run typecheck`

Expected: both commands exit 0.

### Task 2: Benchmark Registry Parity

**Files:**
- Modify: `scripts/benchmark_nagel_pipeline.py`
- Modify: `tools/benchmark_parallel_workers.py`
- Modify: `tests/test_cleaner_backend_registry.py`

- [ ] **Step 1: Add failing parity tests**

Extend `tests/test_cleaner_backend_registry.py` with tests asserting:
- `scripts.benchmark_nagel_pipeline.CLEANER_BACKEND_CHOICES == available_cleaner_backends()`
- `scripts.benchmark_nagel_pipeline.CUDA_CLEANER_QUANTIZATION_CHOICES == CUDA_CLEANER_QUANTIZATIONS`
- `tools.benchmark_parallel_workers` reuses the same benchmark choices.

- [ ] **Step 2: Run the new tests and verify failure**

Run: `pytest tests/test_cleaner_backend_registry.py -q --no-cov`

Expected before implementation: failure because benchmark choices are static duplicates rather than imported registry/config values.

- [ ] **Step 3: Derive benchmark choices from registry/config**

In `scripts/benchmark_nagel_pipeline.py`, import `available_cleaner_backends` and `CUDA_CLEANER_QUANTIZATIONS`, then set:
- `CLEANER_BACKEND_CHOICES = available_cleaner_backends()`
- `CUDA_CLEANER_QUANTIZATION_CHOICES = CUDA_CLEANER_QUANTIZATIONS`

Keep `tools/benchmark_parallel_workers.py` importing those constants from the script.

- [ ] **Step 4: Re-run registry tests**

Run: `pytest tests/test_cleaner_backend_registry.py -q --no-cov`

Expected: exit 0.

### Task 3: Facade Burn-Down and Registry Fixture Cleanup

**Files:**
- Modify: `tests/test_cleaner_backend_registry.py`
- Modify: `tests/test_conversion_service.py`

- [ ] **Step 1: Extend facade burn-down tests**

Add assertions that `modules.tts.cleaner` does not re-export removed MLX/runtime/transport globals:
`mx`, `load`, `generate`, `make_sampler`, `MLX_LM_AVAILABLE`, `urlrequest`, `_ensure_mlx_lm_runtime`, `_get_cached_cleaner`, `_set_cached_cleaner`, `_clear_cleaner_cache`, and `_CleanerAPIHTTPError`.

- [ ] **Step 2: Reuse the registry snapshot fixture**

Change `test_conversion_service_pipeline_config_accepts_registered_cleaner_backend` to accept `registry_snapshot`, import only `register_cleaner_backend`, and remove manual `_REGISTRY` save/restore.

- [ ] **Step 3: Run focused Python tests**

Run:
- `pytest tests/test_cleaner_backend_registry.py -q --no-cov`
- `pytest tests/test_conversion_service.py::test_conversion_service_pipeline_config_accepts_registered_cleaner_backend -q --no-cov`

Expected: both commands exit 0.

### Task 4: Final Verification

**Files:**
- No additional edits expected.

- [ ] **Step 1: Run local verification**

Run:
- `cd desktop && npm test -- --run src/features/conversion/cleanerBackendHelpers.test.ts`
- `cd desktop && npm run typecheck`
- `pytest tests/test_cleaner_backend_registry.py tests/test_conversion_service.py::test_conversion_service_pipeline_config_accepts_registered_cleaner_backend -q --no-cov`

- [ ] **Step 2: Dispatch verification subagents**

Dispatch independent verification agents:
- Desktop verifier: inspect desktop helper tests and run the desktop test/typecheck commands.
- Python verifier: inspect benchmark/facade/fixture changes and run the focused pytest commands.

- [ ] **Step 3: Review verifier reports and final diff**

Confirm verifier outputs against the diff and rerun any failed command after fixes.

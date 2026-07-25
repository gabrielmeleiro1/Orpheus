# Worker Startup Timeout Recovery Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Bound parallel worker startup by one deadline and recover timed-out conversions through the existing sequential fallback.

**Architecture:** `SharedWorkerPoolManager` will wait for the startup-probe set as a group, normalize pending probes into explicit infrastructure failures, and tear down the unusable executor. The existing chapter-runner policy will receive `WorkerInfrastructureError` and continue the current conversion sequentially without changing saved worker settings.

**Tech Stack:** Python 3.12, `concurrent.futures`, pytest, existing pipeline worker lifecycle and fallback test harnesses.

---

### Task 1: Reproduce the timeout classification and cumulative-wait bugs

**Files:**
- Modify: `tests/worker_pool_test_helpers.py`
- Modify: `tests/test_worker_lifecycle.py`

**Step 1: Add a pending startup-probe executor**

Add a `PendingStartupProbeExecutor` test double that returns trackable, never-completing futures for `_worker_startup_probe`, records them, and performs immediate fake shutdown.

**Step 2: Write the failing regression test**

Add `test_startup_probe_timeouts_share_one_deadline_and_are_infrastructure_failures`. Create a four-worker manager with a short startup timeout and an MLX runtime identity, invoke `ensure_started`, and assert:

```python
with pytest.raises(WorkerStartupError) as raised:
    manager.ensure_started(...)

assert elapsed < 0.14
assert len(raised.value.statuses) == 4
assert {status.error_type for status in raised.value.statuses} == {
    "WorkerInfrastructureError"
}
assert all("timed out after 0.05s" in (status.message or "") for status in raised.value.statuses)
assert all(status.provider == "mlx" for status in raised.value.statuses)
assert all(future.cancelled_explicitly for future in executor.pending_probe_futures)
```

**Step 3: Run the test to verify RED**

Run:

```bash
pytest tests/test_worker_lifecycle.py::test_startup_probe_timeouts_share_one_deadline_and_are_infrastructure_failures -q
```

Expected: FAIL because the old loop consumes roughly four timeout windows and produces `TimeoutError` statuses with empty messages.

### Task 2: Apply a shared deadline and explicit timeout status

**Files:**
- Modify: `modules/pipeline/pool_manager.py`
- Test: `tests/test_worker_lifecycle.py`

**Step 1: Implement the minimal pool-manager change**

Import `StartupErrorKind`. In `_run_startup_probes_locked`, call `wait(futures, timeout=self._startup_probe_timeout_seconds)` once. Read results only from completed futures. Cancel pending futures and append one `WorkerStartupStatus` per pending probe with:

```python
WorkerStartupStatus(
    success=False,
    provider=provider,
    device=device,
    error_type=StartupErrorKind.INFRASTRUCTURE,
    message=(
        "worker startup probe timed out after "
        f"{self._startup_probe_timeout_seconds:g}s"
    ),
    phase="startup_probe",
)
```

Resolve `provider` and `device` from the serialized `tts_runtime` entry in the current worker init configuration, falling back to `unknown` and `None`.

**Step 2: Run the focused test to verify GREEN**

Run:

```bash
pytest tests/test_worker_lifecycle.py::test_startup_probe_timeouts_share_one_deadline_and_are_infrastructure_failures -q
```

Expected: PASS.

**Step 3: Run lifecycle and pool tests**

Run:

```bash
pytest tests/test_worker_lifecycle.py tests/test_pool_manager.py -q
```

Expected: PASS with no failures.

### Task 3: Verify conversion-level sequential recovery

**Files:**
- Modify: `tests/test_parallel_fallback.py`

**Step 1: Write a conversion-path regression test**

Add `test_worker_startup_timeout_retries_sequential_via_real_pool_boundary`. Use the pending startup-probe executor with a short-timeout `SharedWorkerPoolManager`, run `_SharedManagerProbePipeline.process_chapters_parallel`, and assert both chapters succeed sequentially, runtime flags are `[False, False]`, and the last fallback payload reports `worker_infrastructure_error`.

**Step 2: Run the test to verify RED if additional wiring is required**

Run:

```bash
pytest tests/test_parallel_fallback.py::test_worker_startup_timeout_retries_sequential_via_real_pool_boundary -q
```

Expected before any necessary wiring: FAIL if the timeout status does not reach the existing infrastructure fallback. If it passes immediately after Task 2, retain it as end-to-end regression coverage because Task 1 already provided the required RED cycle for the production change.

**Step 3: Add only the minimal wiring required**

If the test is red, adjust timeout-status propagation without changing the established fallback policy or persisted manual worker setting.

**Step 4: Run the focused test to verify GREEN**

Run the same focused pytest command. Expected: PASS.

### Task 4: Full verification and review

**Files:**
- Verify: `modules/pipeline/pool_manager.py`
- Verify: `tests/worker_pool_test_helpers.py`
- Verify: `tests/test_worker_lifecycle.py`
- Verify: `tests/test_parallel_fallback.py`

**Step 1: Run targeted regression suites**

```bash
pytest tests/test_worker_lifecycle.py tests/test_pool_manager.py tests/test_parallel_fallback.py tests/test_parallel_pipeline.py tests/test_conversion_service.py -q
```

Expected: PASS.

**Step 2: Run formatting and static checks on changed Python files**

```bash
ruff check modules/pipeline/pool_manager.py tests/worker_pool_test_helpers.py tests/test_worker_lifecycle.py tests/test_parallel_fallback.py
```

Expected: PASS.

**Step 3: Run the repository test command**

```bash
python run_tests.py
```

Expected: PASS with zero failures.

**Step 4: Inspect the final diff and requirements**

Confirm the diff contains no persisted-setting rewrite, no timeout increase, and no unrelated refactor. Verify the one-deadline, explicit-classification, cancellation, observability, and sequential-recovery requirements line by line.

**Step 5: Commit**

```bash
git add modules/pipeline/pool_manager.py tests/worker_pool_test_helpers.py tests/test_worker_lifecycle.py tests/test_parallel_fallback.py docs/plans/2026-07-20-worker-startup-timeout-recovery.md
git commit -m "fix(pipeline): recover from worker startup timeouts"
```

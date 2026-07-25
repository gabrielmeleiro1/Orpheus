# Worker Startup Timeout Recovery Design

## Problem

Parallel conversion starts one model-loaded worker per selected chapter worker. A
manual four-worker MLX run on a 16 GB host timed out while those workers were
loading. The pool waited up to 30 seconds for each probe in sequence, taking
roughly two minutes, then represented the empty `TimeoutError` as an unknown
startup failure. Because unknown failures do not select the existing
infrastructure fallback policy, the complete conversion failed after parsing.

## Behavior

Worker startup probes share one wall-clock deadline. A probe that is still
pending at that deadline produces a `WorkerInfrastructureError` status with an
actionable timeout message. Completed probe results retain their existing
runtime-load, unavailable-runtime, and memory-pressure classifications.

The chapter runner treats the timeout as worker infrastructure instability and
uses its existing sequential fallback. The user's saved manual worker count is
not rewritten; recovery applies only to the current conversion.

## Components and data flow

`SharedWorkerPoolManager` submits the startup probes, waits for all of them
against one deadline, cancels pending probes, records the normalized failure,
and shuts down the unusable pool. `resolve_startup_failure_action` then maps the
explicit infrastructure status to `FALLBACK_INFRASTRUCTURE`.

The current chapter-runner fallback path logs the downgrade, cleans up parallel
state, warms the models in the parent process, and continues chapter processing
sequentially.

## Error handling and observability

The failure status identifies the worker provider when it is available from the
worker configuration and reports the configured deadline. Pool diagnostics keep
recording `startup_failures` and `last_startup_error`, but no longer expose the
ambiguous `unknown [TimeoutError]: worker startup probe failed` message.

## Testing

Regression tests use pending futures to prove that multiple probes consume one
timeout window, timeout statuses are classified as infrastructure failures, the
pool is shut down, and the chapter runner selects sequential fallback. Existing
worker lifecycle, pool manager, parallel fallback, and conversion tests provide
the surrounding regression coverage.

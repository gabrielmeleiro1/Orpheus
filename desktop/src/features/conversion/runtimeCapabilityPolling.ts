import { createRequestTimeoutError, type ApiError } from "../../api/http";
import type { SystemCapabilitiesResponse } from "../../types/api";

const CAPABILITIES_PROBE_POLL_BASE_MS = 1500;
const CAPABILITIES_PROBE_POLL_MAX_MS = 6000;
// RuntimeCapabilityService runs TTS, CUDA cleaner, and MLX cleaner sequentially.
// Each frozen probe can wait 120s for the lock, then run for 120s: 3 * 240s.
// Add 30s for worker cleanup (up to 1s each), snapshot reads, and poll backoff.
export const CAPABILITIES_PROBE_MAX_WAIT_MS = 3 * (120_000 + 120_000) + 30_000;
// /api/system/capabilities reads get_system_runtime_snapshot's cache and starts
// probes in a background thread; it returns pending without awaiting them.
// Allow 8s for that snapshot read, bounded by the remaining overall budget.
export const CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS = 8_000;

type RuntimeProbeSnapshot = Pick<
  SystemCapabilitiesResponse["runtime_probe"],
  "state" | "refresh_in_progress"
>;

type RuntimeCapabilityPollSnapshot = Pick<
  SystemCapabilitiesResponse,
  "cleaner_runtime_availability" | "mlx_runtime_available" | "primary_runtime"
> & {
  runtime_probe?: RuntimeProbeSnapshot | null;
};

function hasUsableTtsCapabilities(capabilities: SystemCapabilitiesResponse | null | undefined): boolean {
  if (!capabilities) return false;
  if (capabilities.mlx_runtime_available === true) return true;
  // The asynchronous TTS probe tests MLX only. CPU/CUDA options do not depend
  // on that probe; their runtime is validated when conversion starts.
  const primaryRuntime = String(capabilities.primary_runtime ?? "").trim().toLowerCase();
  return (primaryRuntime === "cpu" || primaryRuntime === "cuda") && Boolean(capabilities.default_tts_engine.trim());
}

/**
 * True when Settings should keep asking for capabilities so Local MLX can
 * appear: the frozen probe is still settling, or an MLX primary/runtime has
 * published a terminal snapshot while cleaner mlx is still explicitly false.
 */
function shouldKeepPollingForMlxCleaner(
  capabilities: RuntimeCapabilityPollSnapshot | null | undefined,
): boolean {
  if (!capabilities) {
    return false;
  }
  if (capabilities.cleaner_runtime_availability?.mlx !== false) {
    return false;
  }
  if (capabilities.mlx_runtime_available === true) {
    return true;
  }
  return String(capabilities.primary_runtime ?? "").trim().toLowerCase() === "mlx";
}

/**
 * Bounded capability re-fetch gate.
 *
 * The backend's frozen MLX probe finishes asynchronously after startup. Until
 * then `/api/system/capabilities` can report `pending`/`stale` (or
 * `refresh_in_progress`) with local backends marked unavailable. A terminal
 * `fresh` snapshot can also still list `cleaner_runtime_availability.mlx=false`
 * before Setup adoption or a retry flips it — keep polling in that case so the
 * Cleaner selector can expose Local MLX, then stop once resolved or max wait.
 */
export function shouldPollRuntimeCapabilities(
  probe: RuntimeProbeSnapshot | null | undefined,
  capabilities?: RuntimeCapabilityPollSnapshot | null,
): boolean {
  if (!probe) {
    return true;
  }
  if (probe.refresh_in_progress) {
    return true;
  }
  if (probe.state === "pending" || probe.state === "stale") {
    return true;
  }
  return shouldKeepPollingForMlxCleaner(capabilities);
}

interface RuntimeCapabilityPoller {
  /** Record the latest capabilities snapshot (bootstrap or poll). */
  noteCapabilities: (capabilities: SystemCapabilitiesResponse | null | undefined) => void;
  /** True after at least one live poll response was applied. */
  readonly pollApplied: boolean;
  stop: () => void;
}

export interface StartRuntimeCapabilityPollingArgs {
  getSystemCapabilities: (signal: AbortSignal) => Promise<SystemCapabilitiesResponse>;
  onCapabilities: (capabilities: SystemCapabilitiesResponse) => void;
  onError?: (error: ApiError) => void;
  baseMs?: number;
  maxMs?: number;
  maxWaitMs?: number;
}

/**
 * Poll `/api/system/capabilities` with exponential backoff while the runtime
 * probe is non-terminal or MLX cleaner availability is still unresolved on an
 * MLX runtime. Stops on fresh+idle+mlx-ready, max wait, abort, or stop().
 */
export function startRuntimeCapabilityPolling({
  getSystemCapabilities,
  onCapabilities,
  onError,
  baseMs = CAPABILITIES_PROBE_POLL_BASE_MS,
  maxMs = CAPABILITIES_PROBE_POLL_MAX_MS,
  maxWaitMs = CAPABILITIES_PROBE_MAX_WAIT_MS,
}: StartRuntimeCapabilityPollingArgs): RuntimeCapabilityPoller {
  const now = Date.now;
  const setTimeoutFn = (handler: () => void, timeout: number) => globalThis.setTimeout(handler, timeout) as unknown as number;
  const clearTimeoutFn = (handle: number) => {
    globalThis.clearTimeout(handle as unknown as ReturnType<typeof globalThis.setTimeout>);
  };
  let cancelled = false;
  let timer: number | null = null;
  let attempt = 0;
  let pollApplied = false;
  let usableTtsObtained = false;
  let latest: RuntimeCapabilityPollSnapshot | null | undefined;
  let probe: RuntimeProbeSnapshot | null | undefined;
  let controller: AbortController | null = null;
  let budgetTimer: number | null = null;
  const startedAt = now();

  const clearTimer = () => {
    if (timer !== null) {
      clearTimeoutFn(timer);
      timer = null;
    }
  };

  const shouldPoll = () => shouldPollRuntimeCapabilities(probe, latest);

  const clearBudgetTimer = () => {
    if (budgetTimer !== null) {
      clearTimeoutFn(budgetTimer);
      budgetTimer = null;
    }
  };

  const expire = () => {
    cancelled = true;
    clearTimer();
    clearBudgetTimer();
    controller?.abort(new DOMException("request timed out", "TimeoutError"));
    if (!usableTtsObtained) {
      onError?.(createRequestTimeoutError());
    }
  };

  const scheduleNext = () => {
    if (cancelled) return;
    if (!shouldPoll()) {
      clearBudgetTimer();
      return;
    }
    const remaining = maxWaitMs - (now() - startedAt);
    if (remaining <= 0) {
      expire();
      return;
    }
    if (budgetTimer === null) {
      budgetTimer = setTimeoutFn(expire, remaining);
    }
    if (controller) return;
    const delay = Math.min(baseMs * 2 ** attempt, maxMs);
    attempt += 1;
    clearTimer();
    if (delay >= remaining) return;
    timer = setTimeoutFn(() => {
      timer = null;
      void poll();
    }, delay);
  };

  const poll = async () => {
    if (cancelled || !shouldPoll()) return;
    const remaining = maxWaitMs - (now() - startedAt);
    if (remaining <= 0) {
      expire();
      return;
    }
    const requestController = new AbortController();
    controller = requestController;
    const requestTimer = setTimeoutFn(() => {
      requestController.abort(new DOMException("request timed out", "TimeoutError"));
    }, Math.min(CAPABILITIES_PROBE_REQUEST_TIMEOUT_MS, remaining));
    try {
      const capabilities = await getSystemCapabilities(requestController.signal);
      if (cancelled || requestController.signal.aborted) return;
      pollApplied = true;
      usableTtsObtained ||= hasUsableTtsCapabilities(capabilities);
      latest = capabilities;
      probe = capabilities.runtime_probe;
      onCapabilities(capabilities);
    } catch {
      // Retry snapshot failures; exhaustion is fatal only without usable TTS.
    } finally {
      clearTimeoutFn(requestTimer);
      controller = null;
    }
    scheduleNext();
  };

  scheduleNext();

  return {
    noteCapabilities: (capabilities) => {
      usableTtsObtained ||= hasUsableTtsCapabilities(capabilities);
      latest = capabilities;
      probe = capabilities?.runtime_probe;
      if (!shouldPoll()) {
        clearTimer();
        clearBudgetTimer();
      } else if (timer === null) {
        scheduleNext();
      }
    },
    get pollApplied() {
      return pollApplied;
    },
    stop: () => {
      cancelled = true;
      controller?.abort();
      clearTimer();
      clearBudgetTimer();
    },
  };
}

function sameJsonValue(previous: unknown, next: unknown): boolean {
  if (Object.is(previous, next)) return true;
  if (typeof previous !== "object" || typeof next !== "object" || previous === null || next === null) return false;
  if (Array.isArray(previous) !== Array.isArray(next)) return false;
  const previousKeys = Object.keys(previous);
  if (previousKeys.length !== Object.keys(next).length) return false;
  return previousKeys.every((key) => Object.prototype.hasOwnProperty.call(next, key)
    && sameJsonValue((previous as Record<string, unknown>)[key], (next as Record<string, unknown>)[key]));
}

// Counters and live memory readings change on nearly every poll, and no option,
// availability or worker-limit decision reads them. The worker status line shows
// the memory figures from the live snapshot instead (see liveCapabilities.ts).
const VOLATILE_CAPABILITY_FIELDS = new Set([
  "metrics",
  "global_api_limiter",
  "worker_pool",
  "runtime_validation_cache_size",
]);
const VOLATILE_RECOMMENDATION_FIELDS = new Set([
  "total_memory_bytes",
  "available_memory_bytes",
  "reserved_memory_bytes",
  "headroom_memory_bytes",
  "warmup_memory_delta_bytes",
  "estimated_per_worker_memory_bytes",
  "calculation",
]);

function withoutFields(value: unknown, fields: ReadonlySet<string>): unknown {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return value;
  return Object.fromEntries(Object.entries(value).filter(([key]) => !fields.has(key)));
}

function decisionFields(capabilities: SystemCapabilitiesResponse): unknown {
  const stable = withoutFields(capabilities, VOLATILE_CAPABILITY_FIELDS) as Record<string, unknown>;
  if ("parallel_worker_recommendation" in stable) {
    stable.parallel_worker_recommendation = withoutFields(
      stable.parallel_worker_recommendation,
      VOLATILE_RECOMMENDATION_FIELDS,
    );
  }
  return stable;
}

/**
 * Keep the previous snapshot when a poll changed nothing but counters and live
 * memory readings, so the option hooks and the panel do not re-render for them.
 */
export function retainUsedCapabilities(
  previous: SystemCapabilitiesResponse | null,
  next: SystemCapabilitiesResponse,
): SystemCapabilitiesResponse {
  return previous !== null && sameJsonValue(decisionFields(previous), decisionFields(next)) ? previous : next;
}

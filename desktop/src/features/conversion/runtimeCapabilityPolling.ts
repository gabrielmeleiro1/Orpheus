import type { SystemCapabilitiesResponse } from "../../types/api";

export const CAPABILITIES_PROBE_POLL_BASE_MS = 1500;
export const CAPABILITIES_PROBE_POLL_MAX_MS = 6000;
export const CAPABILITIES_PROBE_MAX_WAIT_MS = 60_000;

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

/**
 * True when Settings should keep asking for capabilities so Local MLX can
 * appear: the frozen probe is still settling, or an MLX primary/runtime has
 * published a terminal snapshot while cleaner mlx is still explicitly false.
 */
export function shouldKeepPollingForMlxCleaner(
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

export interface RuntimeCapabilityPoller {
  /** Record the latest capabilities snapshot (bootstrap or poll). */
  noteCapabilities: (capabilities: SystemCapabilitiesResponse | null | undefined) => void;
  /** True after at least one live poll response was applied. */
  readonly pollApplied: boolean;
  stop: () => void;
}

export interface StartRuntimeCapabilityPollingArgs {
  getSystemCapabilities: (signal: AbortSignal) => Promise<SystemCapabilitiesResponse>;
  onCapabilities: (capabilities: SystemCapabilitiesResponse) => void;
  initialCapabilities?: SystemCapabilitiesResponse | null;
  baseMs?: number;
  maxMs?: number;
  maxWaitMs?: number;
  now?: () => number;
  setTimeoutFn?: (handler: () => void, timeout: number) => number;
  clearTimeoutFn?: (handle: number) => void;
}

/**
 * Poll `/api/system/capabilities` with exponential backoff while the runtime
 * probe is non-terminal or MLX cleaner availability is still unresolved on an
 * MLX runtime. Stops on fresh+idle+mlx-ready, max wait, abort, or stop().
 */
export function startRuntimeCapabilityPolling({
  getSystemCapabilities,
  onCapabilities,
  initialCapabilities = undefined,
  baseMs = CAPABILITIES_PROBE_POLL_BASE_MS,
  maxMs = CAPABILITIES_PROBE_POLL_MAX_MS,
  maxWaitMs = CAPABILITIES_PROBE_MAX_WAIT_MS,
  now = Date.now,
  setTimeoutFn = (handler, timeout) => globalThis.setTimeout(handler, timeout) as unknown as number,
  clearTimeoutFn = (handle) => {
    globalThis.clearTimeout(handle as unknown as ReturnType<typeof globalThis.setTimeout>);
  },
}: StartRuntimeCapabilityPollingArgs): RuntimeCapabilityPoller {
  let cancelled = false;
  let timer: number | null = null;
  let attempt = 0;
  let pollApplied = false;
  let latest: RuntimeCapabilityPollSnapshot | null | undefined = initialCapabilities;
  let probe: RuntimeProbeSnapshot | null | undefined = initialCapabilities?.runtime_probe;
  const controller = new AbortController();
  const startedAt = now();

  const clearTimer = () => {
    if (timer !== null) {
      clearTimeoutFn(timer);
      timer = null;
    }
  };

  const shouldPoll = () => shouldPollRuntimeCapabilities(probe, latest);

  const scheduleNext = () => {
    if (cancelled || !shouldPoll()) {
      return;
    }
    if (now() - startedAt >= maxWaitMs) {
      return;
    }
    const delay = Math.min(baseMs * 2 ** attempt, maxMs);
    attempt += 1;
    // Never orphan a pending handle: today scheduleNext only runs after the
    // previous timer fired or was cleared, but overwriting blind would leak one.
    clearTimer();
    timer = setTimeoutFn(() => {
      void poll();
    }, delay);
  };

  const poll = async () => {
    if (cancelled || !shouldPoll()) {
      return;
    }
    try {
      const capabilities = await getSystemCapabilities(controller.signal);
      if (cancelled) {
        return;
      }
      pollApplied = true;
      latest = capabilities;
      probe = capabilities.runtime_probe;
      onCapabilities(capabilities);
    } catch {
      if (cancelled) {
        return;
      }
    }
    scheduleNext();
  };

  scheduleNext();

  return {
    noteCapabilities: (capabilities) => {
      latest = capabilities;
      probe = capabilities?.runtime_probe;
      if (!shouldPoll()) {
        clearTimer();
      } else if (timer === null) {
        scheduleNext();
      }
    },
    get pollApplied() {
      return pollApplied;
    },
    stop: () => {
      cancelled = true;
      controller.abort();
      clearTimer();
    },
  };
}

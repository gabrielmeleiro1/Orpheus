import { useCallback, useEffect, useState } from "react";

import {
  createApiClient,
  createApiClientFromDesktopEndpoint,
  type ApiClient,
} from "../api/client";
import {
  BACKEND_HEALTH_CHECK_INTERVAL_MS,
  describeDesktopBackendStatus,
  readDesktopBackendStatus,
  restartDesktopBackend,
  waitForDesktopEndpoint,
} from "./desktopBackend";
import { isTauriRuntime } from "../platform/tauriRuntime";
import type {
  BackendConnectionState,
  DesktopBackendStatus,
  HealthResponse,
} from "../types/api";

function messageFromUnknown(error: unknown, fallback: string): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  if (typeof error === "string" && error.trim()) {
    return error.trim();
  }
  return fallback;
}

// In browser/dev mode the desktop lifecycle is not observable, so a dead
// backend surfaces only as repeated health-poll failures. We wait for a few
// consecutive failures before declaring an error to avoid flapping on a single
// blip, and recover automatically once a poll succeeds again. The Tauri path
// reuses the same threshold, but the recovery effect only restarts a backend
// whose lifecycle reports "failed" or "stopped"; a hung-but-alive backend
// keeps lifecycle "ready" and is re-bootstrapped, never force-restarted.
const BROWSER_HEALTH_FAILURE_THRESHOLD = 3;

// Bound each health probe so a hung backend surfaces as a failed poll instead
// of an await that never settles (which left the UI in "checking" forever).
const BACKEND_HEALTH_CHECK_TIMEOUT_MS = 5_000;

// Delay before asking the desktop supervisor to relaunch a failed/stopped
// backend and rerunning endpoint acquisition. Keeps repeated crash loops from
// hammering the supervisor while still recovering without a manual reload.
const BACKEND_RECOVERY_RETRY_DELAY_MS = 3_000;

// Cap automatic relaunch attempts so a permanently-broken backend eventually
// stops hammering the supervisor and surfaces a terminal error instead of
// restarting every few seconds forever. The counter resets on any successful
// health connection, so transient crashes still recover indefinitely.
const BACKEND_RECOVERY_MAX_ATTEMPTS = 5;

type BackendRecoveryDecision =
  | { action: "restart"; consumeAttempt: true }
  | { action: "wait"; consumeAttempt: false }
  | { action: "give_up" };

/**
 * Decide whether recovery should restart the desktop backend process.
 * Restart attempts are only consumed for failed/stopped lifecycles; a still-
 * starting backend must keep waiting so a slow boot cannot burn the restart cap.
 */
export function decideBackendRecovery(
  lifecycle: DesktopBackendStatus["lifecycle"],
  recoveryAttempts: number,
  maxAttempts: number = BACKEND_RECOVERY_MAX_ATTEMPTS,
  readyRecoveryAttempts = 0,
): BackendRecoveryDecision {
  if (lifecycle === "failed" || lifecycle === "stopped") {
    if (recoveryAttempts >= maxAttempts) {
      return { action: "give_up" };
    }
    return { action: "restart", consumeAttempt: true };
  }
  if (lifecycle === "ready" && readyRecoveryAttempts >= maxAttempts) {
    return { action: "give_up" };
  }
  return { action: "wait", consumeAttempt: false };
}

// The launcher's final stderr line when startup fails for a cause it can name.
const BACKEND_STARTUP_FAILURE_PREFIX = "failed to start desktop backend: ";

/** Message shown once recovery gives up; a named startup cause stands alone. */
export function describeBackendRecoveryFailure(error: string | null | undefined): string {
  const cause = error?.trim();
  if (cause?.startsWith(BACKEND_STARTUP_FAILURE_PREFIX)) {
    return cause.slice(BACKEND_STARTUP_FAILURE_PREFIX.length);
  }
  return "backend failed to recover after multiple attempts; retry the connection or reload the app"
    + (cause ? `. ${cause}` : "");
}

interface BrowserHealthState {
  failures: number;
  connectionState: BackendConnectionState;
}

export function retainEquivalentHealth(
  previous: HealthResponse | null,
  next: HealthResponse,
): HealthResponse {
  return previous?.status === next.status ? previous : next;
}

export function nextBrowserHealthState(
  previousFailures: number,
  outcome: "success" | "failure",
  threshold: number = BROWSER_HEALTH_FAILURE_THRESHOLD,
): BrowserHealthState {
  if (outcome === "success") {
    return { failures: 0, connectionState: "connected" };
  }
  const failures = previousFailures + 1;
  return {
    failures,
    connectionState: failures >= threshold ? "error" : "checking",
  };
}

interface BackendMonitorFailureResolution {
  clearClient: boolean;
  connectionState: BackendConnectionState;
  health: HealthResponse | null;
  statusMessage: string;
}

export function resolveBackendMonitorFailure(
  status: DesktopBackendStatus | null,
  error: unknown,
  consecutiveFailures = 1,
  failureThreshold: number = BROWSER_HEALTH_FAILURE_THRESHOLD,
): BackendMonitorFailureResolution {
  if (status?.lifecycle === "failed" || status?.lifecycle === "stopped") {
    return {
      clearClient: true,
      connectionState: "error",
      health: null,
      statusMessage: describeDesktopBackendStatus(status),
    };
  }

  // Lifecycle still looks alive but health checks keep failing: after enough
  // consecutive failures, escalate to error so the recovery effect can act.
  if (consecutiveFailures >= failureThreshold) {
    return {
      clearClient: true,
      connectionState: "error",
      health: null,
      statusMessage: messageFromUnknown(error, "Backend health check failed"),
    };
  }

  return {
    clearClient: false,
    connectionState: "checking",
    health: null,
    statusMessage: messageFromUnknown(error, "Backend health check failed"),
  };
}

function readDevBackendOverride(): { baseUrl: string; token: string } | null {
  // Browser dev fallback: pass ?backend=<loopback_base_url>&token=<desktop_token>
  // or set VITE_ORPHEUS_BACKEND_URL / VITE_ORPHEUS_BACKEND_TOKEN.
  const params = new URLSearchParams(window.location.search);
  const queryBaseUrl = params.get("backend")?.trim() ?? "";
  const queryToken = params.get("token")?.trim() ?? "";
  if (queryBaseUrl && queryToken) {
    return { baseUrl: queryBaseUrl, token: queryToken };
  }

  const envBaseUrl = import.meta.env.VITE_ORPHEUS_BACKEND_URL?.trim() ?? "";
  const envToken = import.meta.env.VITE_ORPHEUS_BACKEND_TOKEN?.trim() ?? "";
  if (envBaseUrl && envToken) {
    return { baseUrl: envBaseUrl, token: envToken };
  }

  return null;
}

export function useBackendClient() {
  const [connectionState, setConnectionState] = useState<BackendConnectionState>("checking");
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [statusMessage, setStatusMessage] = useState<string>("Resolving backend endpoint");
  const [backendBaseUrl, setBackendBaseUrl] = useState<string>("");
  const [apiClient, setApiClient] = useState<ApiClient | null>(null);
  const [bootstrapAttempt, setBootstrapAttempt] = useState(0);
  const [recoveryAttempts, setRecoveryAttempts] = useState(0);
  const [readyRecoveryAttempts, setReadyRecoveryAttempts] = useState(0);

  useEffect(() => {
    let cancelled = false;
    // Stops the endpoint wait's IPC polling when this attempt is superseded or unmounted.
    const endpointWait = new AbortController();

    const bootstrap = async () => {
      setConnectionState("checking");
      setHealth(null);
      setStatusMessage("Resolving backend endpoint");

      if (!isTauriRuntime()) {
        const override = readDevBackendOverride();
        if (override) {
          setBackendBaseUrl(override.baseUrl);
          setApiClient(createApiClient({ baseUrl: override.baseUrl, token: override.token }));
          return;
        }

        setApiClient(null);
        setBackendBaseUrl("");
        setConnectionState("error");
        setStatusMessage("Desktop runtime unavailable — launch the desktop app");
        return;
      }

      try {
        const endpoint = await waitForDesktopEndpoint(endpointWait.signal);
        if (cancelled) {
          return;
        }
        if (!endpoint) {
          setApiClient(null);
          setBackendBaseUrl("");
          setConnectionState("error");
          setStatusMessage("Desktop backend startup timed out after 120s");
          return;
        }

        setBackendBaseUrl(endpoint.base_url);
        setApiClient(createApiClientFromDesktopEndpoint(endpoint));
      } catch (error: unknown) {
        if (cancelled) {
          return;
        }
        setApiClient(null);
        setBackendBaseUrl("");
        setConnectionState("error");
        setStatusMessage(messageFromUnknown(error, "Failed to request the desktop backend endpoint"));
      }
    };

    void bootstrap();

    return () => {
      cancelled = true;
      endpointWait.abort();
    };
  }, [bootstrapAttempt]);

  // A failed or stopped backend clears the client but the one-time bootstrap
  // never reran, so recovery used to require reloading the UI. Ask the
  // supervisor to relaunch the process, then rerun endpoint acquisition.
  // Only consume restart attempts when we actually restart; a still-starting
  // backend keeps waiting/re-bootstrapping without burning the restart cap.
  useEffect(() => {
    if (apiClient || connectionState !== "error" || !isTauriRuntime()) {
      return undefined;
    }

    let cancelled = false;
    const timer = window.setTimeout(async () => {
      let decision: BackendRecoveryDecision = { action: "wait", consumeAttempt: false };
      let lifecycle: DesktopBackendStatus["lifecycle"] | null = null;
      try {
        const status = await readDesktopBackendStatus();
        lifecycle = status.lifecycle;
        if (cancelled) {
          return;
        }
        decision = decideBackendRecovery(
          status.lifecycle,
          recoveryAttempts,
          BACKEND_RECOVERY_MAX_ATTEMPTS,
          readyRecoveryAttempts,
        );
        if (decision.action === "give_up") {
          setStatusMessage(describeBackendRecoveryFailure(status.error));
          return;
        }
        if (decision.action === "restart") {
          await restartDesktopBackend();
        }
      } catch {
        // Best effort: bootstrap below reports the failure state either way.
      }
      if (!cancelled) {
        if (decision.action === "restart") {
          setRecoveryAttempts((attempt) => attempt + 1);
          setReadyRecoveryAttempts(0);
        } else if (lifecycle === "ready") {
          setReadyRecoveryAttempts((attempt) => attempt + 1);
        }
        setBootstrapAttempt((attempt) => attempt + 1);
      }
    }, BACKEND_RECOVERY_RETRY_DELAY_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [apiClient, bootstrapAttempt, connectionState, readyRecoveryAttempts, recoveryAttempts]);

  useEffect(() => {
    if (!apiClient || !backendBaseUrl) {
      return;
    }

    let cancelled = false;
    let intervalId: number | null = null;
    let healthFailures = 0;
    // Guard against ticks piling up while a probe is still in flight (e.g. a
    // slow backend close to the timeout); overlapping checks would race their
    // state updates and inflate the failure count.
    let checkInFlight = false;

    const markBackendUnavailable = (message: string) => {
      if (cancelled) {
        return;
      }
      setHealth(null);
      setApiClient(null);
      setBackendBaseUrl("");
      setConnectionState("error");
      setStatusMessage(message);
    };

    const checkBackend = async () => {
      if (checkInFlight) {
        return;
      }
      checkInFlight = true;
      let status: Awaited<ReturnType<typeof readDesktopBackendStatus>> | null = null;

      try {
        status = isTauriRuntime() ? await readDesktopBackendStatus() : { lifecycle: "ready", endpoint: null, error: null };
        if (cancelled) {
          return;
        }
        if (status.lifecycle === "failed" || status.lifecycle === "stopped") {
          markBackendUnavailable(describeDesktopBackendStatus(status));
          return;
        }
        if (status.lifecycle !== "ready") {
          setHealth(null);
          setConnectionState("checking");
          setStatusMessage(describeDesktopBackendStatus(status));
          return;
        }
        const liveBaseUrl = status.endpoint?.base_url;
        if (liveBaseUrl && liveBaseUrl !== backendBaseUrl) {
          // The supervisor relaunched the backend on a new socket and token.
          // This client can only fail from here on, so acquire the new
          // endpoint now instead of waiting out the health-failure threshold.
          setHealth(null);
          setApiClient(null);
          setBackendBaseUrl("");
          setConnectionState("checking");
          setStatusMessage("Backend restarted; reconnecting");
          setBootstrapAttempt((attempt) => attempt + 1);
          return;
        }

        const response = await apiClient.getHealth({
          timeoutMs: BACKEND_HEALTH_CHECK_TIMEOUT_MS,
        });
        if (cancelled) {
          return;
        }
        healthFailures = 0;
        setRecoveryAttempts(0);
        setReadyRecoveryAttempts(0);
        setHealth((previous) => retainEquivalentHealth(previous, response));
        setConnectionState("connected");
        setStatusMessage("Backend connected");
      } catch (error: unknown) {
        if (cancelled) {
          return;
        }
        if (!isTauriRuntime()) {
          const outcome = nextBrowserHealthState(healthFailures, "failure");
          healthFailures = outcome.failures;
          setHealth(null);
          setConnectionState(outcome.connectionState);
          setStatusMessage(
            outcome.connectionState === "error"
              ? messageFromUnknown(error, "Backend health check failed")
              : `Checking ${backendBaseUrl}/health`,
          );
          return;
        }
        healthFailures += 1;
        const resolution = resolveBackendMonitorFailure(status, error, healthFailures);
        if (resolution.clearClient) {
          markBackendUnavailable(resolution.statusMessage);
          return;
        }
        setHealth(resolution.health);
        setConnectionState(resolution.connectionState);
        setStatusMessage(resolution.statusMessage);
      } finally {
        checkInFlight = false;
      }
    };

    setConnectionState("checking");
    setStatusMessage(`Checking ${backendBaseUrl}/health`);

    void checkBackend();
    intervalId = window.setInterval(() => {
      void checkBackend();
    }, BACKEND_HEALTH_CHECK_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (intervalId !== null) {
        window.clearInterval(intervalId);
      }
    };
  }, [apiClient, backendBaseUrl]);

  const reconnectBackend = useCallback(() => {
    setRecoveryAttempts(0);
    setReadyRecoveryAttempts(0);
    setApiClient(null);
    setBackendBaseUrl("");
    setHealth(null);
    setConnectionState("checking");
    setStatusMessage("Reconnecting to the restarted backend");
    setBootstrapAttempt((attempt) => attempt + 1);
  }, []);

  return {
    apiClient,
    backendBaseUrl,
    connectionState,
    health,
    statusMessage,
    reconnectBackend,
  };
}

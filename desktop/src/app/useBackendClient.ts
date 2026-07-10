import { useEffect, useState } from "react";

import {
  createApiClient,
  createApiClientFromDesktopEndpoint,
  type ApiClient,
  type DesktopBackendStatus,
} from "../api/client";
import {
  BACKEND_HEALTH_CHECK_INTERVAL_MS,
  describeDesktopBackendStatus,
  readDesktopBackendStatus,
  waitForDesktopEndpoint,
} from "../features/dashboard/dashboardHelpers";
import { isTauriRuntime } from "../platform/tauriRuntime";
import type {
  BackendConnectionState,
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
// blip, and recover automatically once a poll succeeds again.
export const BROWSER_HEALTH_FAILURE_THRESHOLD = 3;

export interface BrowserHealthState {
  failures: number;
  connectionState: BackendConnectionState;
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

export interface BackendMonitorFailureResolution {
  clearClient: boolean;
  connectionState: BackendConnectionState;
  health: HealthResponse | null;
  statusMessage: string;
}

export function resolveBackendMonitorFailure(
  status: DesktopBackendStatus | null,
  error: unknown,
): BackendMonitorFailureResolution {
  if (status?.lifecycle === "failed" || status?.lifecycle === "stopped") {
    return {
      clearClient: true,
      connectionState: "error",
      health: null,
      statusMessage: describeDesktopBackendStatus(status),
    };
  }

  return {
    clearClient: false,
    connectionState: "checking",
    health: null,
    statusMessage: messageFromUnknown(error, "backend health check failed"),
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
  const [statusMessage, setStatusMessage] = useState<string>("resolving backend endpoint");
  const [backendBaseUrl, setBackendBaseUrl] = useState<string>("");
  const [apiClient, setApiClient] = useState<ApiClient | null>(null);

  useEffect(() => {
    let cancelled = false;

    const bootstrap = async () => {
      setConnectionState("checking");
      setHealth(null);
      setStatusMessage("resolving backend endpoint");

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
        setStatusMessage("desktop runtime unavailable; launch the Tauri app");
        return;
      }

      try {
        const endpoint = await waitForDesktopEndpoint();
        if (cancelled) {
          return;
        }
        if (!endpoint) {
          setApiClient(null);
          setBackendBaseUrl("");
          setConnectionState("error");
          setStatusMessage("desktop backend startup timed out after 60s");
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
        setStatusMessage(messageFromUnknown(error, "failed to request desktop backend endpoint"));
      }
    };

    void bootstrap();

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!apiClient || !backendBaseUrl) {
      return;
    }

    let cancelled = false;
    let intervalId: number | null = null;
    let browserHealthFailures = 0;

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

        const response = await apiClient.getHealth();
        if (cancelled) {
          return;
        }
        browserHealthFailures = 0;
        setHealth(response);
        setConnectionState("connected");
        setStatusMessage("backend connected");
      } catch (error: unknown) {
        if (cancelled) {
          return;
        }
        if (!isTauriRuntime()) {
          const outcome = nextBrowserHealthState(browserHealthFailures, "failure");
          browserHealthFailures = outcome.failures;
          setHealth(null);
          setConnectionState(outcome.connectionState);
          setStatusMessage(
            outcome.connectionState === "error"
              ? messageFromUnknown(error, "backend health check failed")
              : `checking ${backendBaseUrl}/health`,
          );
          return;
        }
        const resolution = resolveBackendMonitorFailure(status, error);
        setHealth(resolution.health);
        setConnectionState(resolution.connectionState);
        setStatusMessage(resolution.statusMessage);
        if (resolution.clearClient) {
          setApiClient(null);
          setBackendBaseUrl("");
        }
      }
    };

    setConnectionState("checking");
    setStatusMessage(`checking ${backendBaseUrl}/health`);

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

  return {
    apiClient,
    backendBaseUrl,
    connectionState,
    health,
    statusMessage,
  };
}

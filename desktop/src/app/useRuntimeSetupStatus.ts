import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { ApiError, type ApiClient } from "../api/client";
import { useGenerationGuard } from "../state/generationGuard";
import { isCheckingConfiguredRuntime, shouldShowRuntimeSetup } from "../features/setup/setupModel";
import { syncSetupPreferences } from "../features/setup/setupPreferences";
import type { RuntimeSetupStatusResponse } from "../types/api";
import { invalidateRuntimeBootstrap } from "./runtimeBootstrapCache";
import { readReadyRuntimeHint, rememberReadyRuntime } from "./runtimeStartup";

// Allow time for the cold-start subprocess probe and first-time model hashing.
const SETUP_STATUS_TIMEOUT_MS = 120_000;
const SETUP_STATUS_RETRY_MS = 2_000;
const SETUP_STATUS_MAX_RETRIES = 60;
const SETUP_ERROR_MAX_CHARS = 512;

/**
 * Loads the runtime setup status for the current backend client and keeps
 * checking while the configured runtime is still being verified.
 */
export function useRuntimeSetupStatus(apiClient: ApiClient | null) {
  const [readyRuntimeHint] = useState(readReadyRuntimeHint);
  const [setupStatus, setSetupStatus] = useState<RuntimeSetupStatusResponse | null>(null);
  const [setupStatusClient, setSetupStatusClient] = useState<ApiClient | null>(null);
  const [setupError, setSetupError] = useState<string | null>(null);
  const [checkExhausted, setCheckExhausted] = useState(false);
  const [checkAttempt, setCheckAttempt] = useState(0);
  const [libraryOnly, setLibraryOnly] = useState(false);
  const [runtimeSelectionGeneration, setRuntimeSelectionGeneration] = useState(0);
  const setupStatusRef = useRef(setupStatus);
  // A status applied after a Settings change is newer than any read already in flight.
  const appliedGuard = useGenerationGuard();
  const inFlightReadRef = useRef<AbortController | null>(null);

  const applySetupStatus = useCallback((status: RuntimeSetupStatusResponse, nextLibraryOnly: boolean) => {
    appliedGuard.invalidate();
    inFlightReadRef.current?.abort();
    setupStatusRef.current = status;
    rememberReadyRuntime(status);
    syncSetupPreferences(status);
    if (apiClient) invalidateRuntimeBootstrap(apiClient);
    setRuntimeSelectionGeneration((previous) => previous + 1);
    setSetupStatusClient(apiClient);
    setSetupStatus(status);
    // An authoritative status ends any stalled or failed startup check.
    setSetupError(null);
    setCheckExhausted(false);
    setLibraryOnly(nextLibraryOnly);
  }, [apiClient, appliedGuard]);

  const onSetupReady = useCallback(
    (status: RuntimeSetupStatusResponse) => applySetupStatus(status, false),
    [applySetupStatus],
  );

  // Model changes made in Settings keep the user in Settings: a runtime that is
  // no longer ready limits conversions instead of reopening first-run setup.
  const onSettingsSetupReady = useCallback(
    (status: RuntimeSetupStatusResponse) => applySetupStatus(status, shouldShowRuntimeSetup(status, false)),
    [applySetupStatus],
  );

  const continueLibraryOnly = useCallback(() => setLibraryOnly(true), []);
  const retryCheck = useCallback(() => setCheckAttempt((previous) => previous + 1), []);

  useLayoutEffect(() => {
    setupStatusRef.current = setupStatus;
  }, [setupStatus]);

  useEffect(() => {
    if (!apiClient) return undefined;
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    setSetupError(null);
    setCheckExhausted(false);

    const retryOrGiveUp = (attempt: number) => {
      if (attempt < SETUP_STATUS_MAX_RETRIES) {
        retryTimer = setTimeout(() => loadStatus(attempt + 1), SETUP_STATUS_RETRY_MS);
      } else {
        setCheckExhausted(true);
      }
    };
    // A read superseded by an applied status is dropped; keep verifying only if the
    // applied status itself is still being checked.
    const resumeAfterApplied = (attempt: number) => {
      if (isCheckingConfiguredRuntime(setupStatusRef.current)) retryOrGiveUp(attempt);
    };
    const loadStatus = (attempt: number) => {
      const isCurrent = appliedGuard.capture();
      // A status read can wait minutes for a cold probe; release it with its client.
      const controller = new AbortController();
      inFlightReadRef.current = controller;
      void apiClient.getRuntimeSetupStatus({ timeoutMs: SETUP_STATUS_TIMEOUT_MS, signal: controller.signal }).then((status) => {
        if (!active) return;
        if (!isCurrent()) {
          resumeAfterApplied(attempt);
          return;
        }
        rememberReadyRuntime(status);
        if (syncSetupPreferences(status)) {
          invalidateRuntimeBootstrap(apiClient);
          setRuntimeSelectionGeneration((previous) => previous + 1);
        }
        setSetupStatusClient(apiClient);
        setSetupStatus(status);
        // Keep readiness unverified while returning users browse the dashboard.
        if (isCheckingConfiguredRuntime(status)) retryOrGiveUp(attempt);
      }).catch((error: unknown) => {
        if (!active) return;
        if (!isCurrent()) {
          resumeAfterApplied(attempt);
          return;
        }
        // A lost backend is a connection problem, which the dashboard's backend
        // badge reports; reopening setup would misstate it. That holds after a
        // verified load, and for a returning user whose first check never got
        // an answer. Keep checking so the status recovers with the backend.
        const unreachable = !(error instanceof ApiError) || error.status === null;
        if (setupStatusRef.current || (readyRuntimeHint && unreachable)) {
          retryOrGiveUp(attempt);
          return;
        }
        const message = error instanceof Error && error.message.trim()
          ? error.message.trim().slice(0, SETUP_ERROR_MAX_CHARS)
          : "Runtime setup status is temporarily unavailable.";
        setSetupStatus(null);
        setSetupError(message);
      }).finally(() => {
        if (inFlightReadRef.current === controller) inFlightReadRef.current = null;
      });
    };
    loadStatus(0);
    return () => {
      active = false;
      inFlightReadRef.current?.abort();
      inFlightReadRef.current = null;
      if (retryTimer !== undefined) clearTimeout(retryTimer);
    };
  }, [apiClient, readyRuntimeHint, checkAttempt, appliedGuard]);

  return {
    readyRuntimeHint,
    /** Last known status; survives a backend restart so screen gating stays put. */
    setupStatus,
    /** Status verified against the current client, or null until it is. */
    currentSetupStatus: setupStatusClient === apiClient ? setupStatus : null,
    setupError,
    checkExhausted,
    libraryOnly,
    runtimeSelectionGeneration,
    onSetupReady,
    onSettingsSetupReady,
    continueLibraryOnly,
    retryCheck,
  };
}

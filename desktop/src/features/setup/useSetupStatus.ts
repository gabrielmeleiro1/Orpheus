import { useCallback, useEffect, useLayoutEffect, useRef, useState, type SetStateAction } from "react";

import type { ApiClient } from "../../api/client";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { createSelection, selectionMatchesConfigured, type SetupSelection } from "./setupModel";
import { safeError } from "./setupPageHelpers";

interface UseSetupStatusArgs {
  apiClient: ApiClient | null;
  initialStatus: RuntimeSetupStatusResponse | null;
  initialError: string | null;
  onReady: (status: RuntimeSetupStatusResponse) => void;
  preserveSelection?: boolean;
}

export function useSetupStatus({ apiClient, initialStatus, initialError, onReady, preserveSelection = false }: UseSetupStatusArgs) {
  const [status, setStatus] = useState(initialStatus);
  const [error, setError] = useState(initialError);
  const [selection, updateSelection] = useState<SetupSelection>(() => initialStatus
    ? createSelection(initialStatus)
    : { profile: "base", modelGroups: [] });
  const statusRef = useRef(status);
  useLayoutEffect(() => {
    statusRef.current = status;
  });
  const selectionRef = useRef(selection);
  useLayoutEffect(() => {
    selectionRef.current = selection;
  });
  const clientRef = useRef(apiClient);
  useLayoutEffect(() => {
    clientRef.current = apiClient;
  });
  const requestSequence = useRef(0);
  const previousClient = useRef(apiClient);
  const selectionDirty = useRef(false);
  const setSelection = useCallback((next: SetStateAction<SetupSelection>) => {
    selectionDirty.current = true;
    updateSelection(next);
  }, []);

  useEffect(() => {
    // A replacement client or parent snapshot supersedes pending status reads.
    requestSequence.current += 1;
    const clientChanged = previousClient.current !== apiClient;
    const incompatible = initialStatus && !initialStatus.profiles.some((profile) => profile.id === selectionRef.current.profile && profile.compatible);
    if (clientChanged || incompatible) selectionDirty.current = false;
    const keepDraft = selectionDirty.current && !clientChanged && !incompatible;
    previousClient.current = apiClient;
    setStatus(initialStatus);
    setError(initialError);
    if (initialStatus && !keepDraft) updateSelection(createSelection(initialStatus));
    return () => { requestSequence.current += 1; };
  }, [apiClient, initialError, initialStatus, preserveSelection]);

  const refreshStatus = useCallback(async () => {
    if (!apiClient) {
      setError("Runtime setup status is temporarily unavailable.");
      return null;
    }
    const sequence = ++requestSequence.current;
    const isCurrent = () => clientRef.current === apiClient && requestSequence.current === sequence;
    try {
      // Frozen status reads return pending while cold probes continue in the backend.
      const next = await apiClient.getRuntimeSetupStatus({ timeoutMs: 120_000 });
      if (!isCurrent()) return null;
      const incompatible = !next.profiles.some((profile) => profile.id === selectionRef.current.profile && profile.compatible);
      if (incompatible || (!statusRef.current && !selectionDirty.current)) {
        selectionDirty.current = false;
        updateSelection(createSelection(next));
      }
      setStatus(next);
      setError(null);
      const draftChanged = preserveSelection && statusRef.current?.configured
        && !selectionMatchesConfigured(selectionRef.current, statusRef.current);
      if (next.ready && next.state === "ready" && !next.restart_required && !draftChanged) onReady(next);
      return next;
    } catch (reason: unknown) {
      if (isCurrent()) setError(safeError(reason, "Runtime setup status is temporarily unavailable."));
      return null;
    }
  }, [apiClient, onReady, preserveSelection]);

  const checking = Boolean(status?.checking || status?.installed_runtime?.checking
    || status?.probe.checks.some((check) => check.state === "timeout" || check.state === "pending"));
  const refreshRef = useRef(refreshStatus);
  useLayoutEffect(() => {
    refreshRef.current = refreshStatus;
  });
  useEffect(() => {
    if (!apiClient || !checking) return;
    let active = true;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      attempts += 1;
      const next = await refreshRef.current();
      if (active && attempts < 120 && next && (next.checking || next.installed_runtime?.checking
        || next.probe.checks.some((check) => check.state === "timeout" || check.state === "pending"))) {
        timer = setTimeout(() => void poll(), 3_000);
      }
    };
    timer = setTimeout(() => void poll(), 3_000);
    return () => { active = false; clearTimeout(timer); };
  }, [apiClient, checking]);

  return { status, setStatus, selection, setSelection, error, setError, refreshStatus };
}

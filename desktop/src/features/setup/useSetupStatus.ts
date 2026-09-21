import { useCallback, useEffect, useRef, useState } from "react";

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
  const [selection, setSelection] = useState<SetupSelection>(() => initialStatus
    ? createSelection(initialStatus)
    : { profile: "base", modelGroups: [], acceptPytorchRisk: false });
  const statusRef = useRef(status);
  statusRef.current = status;
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  const clientRef = useRef(apiClient);
  clientRef.current = apiClient;
  const requestSequence = useRef(0);
  const previousClient = useRef(apiClient);

  useEffect(() => {
    // A replacement client or parent snapshot supersedes pending status reads.
    requestSequence.current += 1;
    const keepDraft = preserveSelection && previousClient.current === apiClient && statusRef.current
      && !selectionMatchesConfigured(selectionRef.current, statusRef.current);
    previousClient.current = apiClient;
    setStatus(initialStatus);
    setError(initialError);
    if (initialStatus && !keepDraft) setSelection(createSelection(initialStatus));
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
      // Cold probes and first-time model hashing share the app's two-minute budget.
      const next = await apiClient.getRuntimeSetupStatus({ timeoutMs: 120_000 });
      if (!isCurrent()) return null;
      if (!statusRef.current) setSelection(createSelection(next));
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

  return { status, setStatus, selection, setSelection, error, setError, refreshStatus };
}

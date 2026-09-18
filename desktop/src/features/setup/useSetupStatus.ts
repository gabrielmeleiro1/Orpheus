import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { createSelection, type SetupSelection } from "./setupModel";
import { safeError } from "./setupPageHelpers";

interface UseSetupStatusArgs {
  apiClient: ApiClient | null;
  initialStatus: RuntimeSetupStatusResponse | null;
  initialError: string | null;
  onReady: (status: RuntimeSetupStatusResponse) => void;
}

export function useSetupStatus({ apiClient, initialStatus, initialError, onReady }: UseSetupStatusArgs) {
  const [status, setStatus] = useState(initialStatus);
  const [error, setError] = useState(initialError);
  const [selection, setSelection] = useState<SetupSelection>(() => initialStatus
    ? createSelection(initialStatus)
    : { profile: "base", modelGroups: [], acceptPytorchRisk: false });
  const statusRef = useRef(status);
  statusRef.current = status;
  const clientRef = useRef(apiClient);
  clientRef.current = apiClient;
  const requestSequence = useRef(0);

  useEffect(() => {
    // A replacement client or parent snapshot supersedes pending status reads.
    requestSequence.current += 1;
    setStatus(initialStatus);
    setError(initialError);
    if (initialStatus) setSelection(createSelection(initialStatus));
    return () => { requestSequence.current += 1; };
  }, [apiClient, initialError, initialStatus]);

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
      if (next.ready && next.state === "ready" && !next.restart_required) onReady(next);
      return next;
    } catch (reason: unknown) {
      if (isCurrent()) setError(safeError(reason, "Runtime setup status is temporarily unavailable."));
      return null;
    }
  }, [apiClient, onReady]);

  return { status, setStatus, selection, setSelection, error, setError, refreshStatus };
}

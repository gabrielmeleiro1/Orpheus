import { useEffect, useRef, type Dispatch, type MutableRefObject, type SetStateAction } from "react";

import type { ApiClient } from "../../api/client";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import type { RuntimeSetupEnvironment } from "./setupBridge";
import {
  applyProgressEvent,
  modelDownloadProgressEvent,
  type SetupProgressState,
  type SetupSelection,
} from "./setupModel";
import { EMPTY_PROGRESS, safeError } from "./setupPageHelpers";

export type SetupDownloadPhase = "idle" | "downloading" | "adopting";

interface UseSetupModelDownloadArgs {
  apiClient: ApiClient | null;
  environment: RuntimeSetupEnvironment | null;
  downloadPhase: SetupDownloadPhase;
  selection: SetupSelection;
  onReady: (status: RuntimeSetupStatusResponse) => void;
  setDownloadPhase: Dispatch<SetStateAction<SetupDownloadPhase>>;
  setBusy: Dispatch<SetStateAction<boolean>>;
  setError: Dispatch<SetStateAction<string | null>>;
  setProgress: Dispatch<SetStateAction<SetupProgressState>>;
  setStatus: Dispatch<SetStateAction<RuntimeSetupStatusResponse | null>>;
  startLocked: MutableRefObject<boolean>;
  lastAttempt: MutableRefObject<"backend" | "bridge" | null>;
}

/** Resume and monitor packaged model downloads, then verify the runtime. */
export function useSetupModelDownload({
  apiClient,
  environment,
  downloadPhase,
  selection,
  onReady,
  setDownloadPhase,
  setBusy,
  setError,
  setProgress,
  setStatus,
  startLocked,
  lastAttempt,
}: UseSetupModelDownloadArgs): void {
  const latest = useRef({ selection, onReady });
  latest.current = { selection, onReady };
  const completedPlan = useRef<SetupSelection | null>(null);
  // A download that survived a page remount (Settings re-entry, reload) must
  // resume driving the progress UI instead of allowing a second start.
  useEffect(() => {
    if (environment === null || environment.available || !apiClient) {
      return undefined;
    }
    if (typeof apiClient.getRuntimeModelDownloadStatus !== "function") {
      return undefined;
    }
    let active = true;
    void apiClient.getRuntimeModelDownloadStatus({ timeoutMs: 8_000 }).then((snapshot) => {
      if (!active || !["running", "stopping"].includes(snapshot.lifecycle)) return;
      lastAttempt.current = "backend";
      startLocked.current = true;
      setBusy(true);
      setDownloadPhase("downloading");
      setProgress((previous) => applyProgressEvent(previous, modelDownloadProgressEvent(snapshot)));
    }).catch(() => {
      // Backends without the download endpoint (or transient failures) just
      // leave the flow idle; starting a download will surface a real error.
    });
    return () => {
      active = false;
    };
  }, [apiClient, environment]);

  useEffect(() => {
    if (downloadPhase !== "downloading" || !apiClient) {
      return undefined;
    }
    let active = true;
    let pollFailures = 0;
    const tick = async () => {
      let snapshot;
      try {
        snapshot = await apiClient.getRuntimeModelDownloadStatus({ timeoutMs: 8_000 });
      } catch {
        // Tolerate transient poll failures, but a backend that stays
        // unreachable must surface instead of spinning forever.
        pollFailures += 1;
        if (active && pollFailures >= 8) {
          const message = "Lost connection to the installer. The download may still finish in the background — retry to check.";
          setDownloadPhase("idle");
          startLocked.current = false;
          setBusy(false);
          setError(message);
          setProgress((previous) => ({ ...previous, lifecycle: "failed", message }));
        }
        return;
      }
      if (!active) return;
      pollFailures = 0;
      setProgress((previous) => applyProgressEvent(previous, modelDownloadProgressEvent(snapshot)));
      if (snapshot.lifecycle === "complete") {
        const selected = latest.current.selection;
        completedPlan.current = {
          profile: snapshot.profile === "mlx" || snapshot.profile === "cuda" ? snapshot.profile : selected.profile,
          modelGroups: [...snapshot.model_groups],
          acceptPytorchRisk: snapshot.profile === selected.profile && selected.acceptPytorchRisk,
        };
        setDownloadPhase("adopting");
      } else if (snapshot.lifecycle === "cancelled") {
        setDownloadPhase("idle");
        startLocked.current = false;
        setBusy(false);
        setProgress({ ...EMPTY_PROGRESS, message: "Model download cancelled." });
      } else if (snapshot.lifecycle === "failed" || snapshot.lifecycle === "idle") {
        const message = snapshot.lifecycle === "failed"
          ? snapshot.error ?? "Model download failed."
          : "Model download state was lost. Try again.";
        setDownloadPhase("idle");
        startLocked.current = false;
        setBusy(false);
        setError(message);
        if (snapshot.lifecycle === "idle") {
          setProgress((previous) => ({ ...previous, lifecycle: "failed", message }));
        }
      }
    };
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      await tick();
      if (active) timer = setTimeout(() => void poll(), 1_000);
    };
    void poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [apiClient, downloadPhase]);

  useEffect(() => {
    if (downloadPhase !== "adopting" || !apiClient) {
      return undefined;
    }
    let active = true;
    // Freeze the completed download plan; parent renders must not submit adoption again.
    const plan = completedPlan.current ?? latest.current.selection;
    setProgress((previous) => applyProgressEvent(previous, {
      event: "status",
      stage: "verify",
      message: "Verifying and adopting the runtime…",
    }));
    void apiClient.adoptRuntimeSetup(
      {
        profile: plan.profile,
        model_groups: [...plan.modelGroups],
        accept_pytorch_risk: plan.acceptPytorchRisk,
      },
      { timeoutMs: 30_000 },
    ).then((next) => {
      if (!active) return;
      setDownloadPhase("idle");
      startLocked.current = false;
      setBusy(false);
      setStatus(next);
      if (next.ready && next.state === "ready" && !next.restart_required) {
        setProgress((previous) => applyProgressEvent(previous, {
          event: "complete",
          terminal: true,
          message: "Local runtime ready.",
        }));
        latest.current.onReady(next);
      } else {
        setError("The runtime did not become ready after download. Retry, or continue in library-only mode.");
      }
    }).catch((reason: unknown) => {
      if (!active) return;
      setDownloadPhase("idle");
      startLocked.current = false;
      setBusy(false);
      setError(safeError(reason, "The runtime could not be adopted after download."));
    });
    return () => {
      active = false;
    };
  }, [apiClient, downloadPhase]);
}

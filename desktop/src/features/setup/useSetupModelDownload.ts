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

export type SetupDownloadPhase = "idle" | "starting" | "downloading" | "adopting";

interface UseSetupModelDownloadArgs {
  apiClient: ApiClient | null;
  environment: RuntimeSetupEnvironment | null;
  downloadPhase: SetupDownloadPhase;
  selection: SetupSelection;
  onReady: (status: RuntimeSetupStatusResponse) => void;
  onModelDownloadReady?: (status: RuntimeSetupStatusResponse) => void;
  onDownloadingModel?: (groupId: string | null) => void;
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
  onModelDownloadReady,
  onDownloadingModel,
  setDownloadPhase,
  setBusy,
  setError,
  setProgress,
  setStatus,
  startLocked,
  lastAttempt,
}: UseSetupModelDownloadArgs): void {
  const latest = useRef({ selection, onReady, onModelDownloadReady, onDownloadingModel });
  latest.current = { selection, onReady, onModelDownloadReady, onDownloadingModel };
  const configureRuntime = useRef(true);
  const completedPlan = useRef<SetupSelection | null>(null);
  // A download that survived a page remount (Settings re-entry, reload) must
  // resume driving the progress UI instead of allowing a second start.
  useEffect(() => {
    if (environment === null || !apiClient) {
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
      latest.current.onDownloadingModel?.(snapshot.current_model_group ?? snapshot.model_groups[0] ?? null);
      setProgress((previous) => applyProgressEvent(previous, modelDownloadProgressEvent(snapshot)));
      if (snapshot.lifecycle === "complete") {
        configureRuntime.current = snapshot.configure_runtime !== false;
        const selected = latest.current.selection;
        completedPlan.current = {
          profile: snapshot.profile === "mlx" || snapshot.profile === "cuda" ? snapshot.profile : selected.profile,
          modelGroups: [...snapshot.model_groups],
          cleanerCudaQuantization: snapshot.cleaner_cuda_quantization ?? selected.cleanerCudaQuantization ?? "4bit",
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
      message: configureRuntime.current ? "Verifying and adopting the runtime…" : "Checking the downloaded model…",
    }));
    const request = configureRuntime.current ? apiClient.adoptRuntimeSetup(
      {
        profile: plan.profile,
        model_groups: [...plan.modelGroups],
        accept_pytorch_risk: plan.acceptPytorchRisk,
        cleaner_cuda_quantization: plan.cleanerCudaQuantization ?? "4bit",
      },
      { timeoutMs: 120_000 },
    ) : apiClient.getRuntimeSetupStatus({ timeoutMs: 120_000 });
    void request.then((next) => {
      if (!active) return;
      setDownloadPhase("idle");
      startLocked.current = false;
      setBusy(false);
      setStatus(next);
      if (!configureRuntime.current) {
        const groups = next.installed_runtime?.profile === plan.profile ? next.installed_runtime.model_groups : next.selected_model_groups;
        if (!plan.modelGroups.every((id) => groups.some((group) => group.id === id && group.assets_ready))) {
          setError("The download finished, but the model could not be verified. Check runtime and try again.");
          setProgress((previous) => ({ ...previous, lifecycle: "failed", message: "Model verification failed." }));
          latest.current.onModelDownloadReady?.(next);
          return;
        }
        setProgress((previous) => applyProgressEvent(previous, { event: "complete", terminal: true, message: "Model installed. Choose the models you want to use, then save." }));
        latest.current.onModelDownloadReady?.(next);
        return;
      }
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
      setError(safeError(reason, configureRuntime.current ? "The runtime could not be adopted after download." : "The downloaded model could not be verified. Check runtime to retry."));
    });
    return () => {
      active = false;
    };
  }, [apiClient, downloadPhase]);
}

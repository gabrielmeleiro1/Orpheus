import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { BrailleSpinner } from "../../ui/ascii/BrailleSpinner";
import { RUNTIME_LABELS } from "../../format/runtime";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { invalidateRuntimeBootstrap } from "../../app/runtimeBootstrapCache";
import {
  cancelRuntimeSetup,
  getRuntimeSetupEnvironment,
  getRuntimeSetupRunnerStatus,
  listenRuntimeSetupProgress,
  restartOrpheusBackend,
  startRuntimeSetup,
  type RuntimeSetupRequest,
  type RuntimeSetupEnvironment,
  type RuntimeSetupRunnerStatus,
} from "./setupBridge";
import {
  applyProgressEvent,
  applyRunnerStatus,
  acquireSetupStart,
  backendInstallProfileId,
  canStartSetup,
  MODEL_GROUP_DETAILS,
  createSelection,
  diagnosticsSnapshot,
  modelDownloadProgressEvent,
  profileChoices,
  selectionEstimate,
  selectionMatchesConfigured,
  selectionReadiness,
  type SetupProgressEvent,
  type SetupProgressState,
} from "./setupModel";
import { restartAndReconnectSetup } from "./setupLifecycle";
import {
  EMPTY_PROGRESS,
  safeError,
} from "./setupPageHelpers";
import { SetupDangerZone } from "./SetupDangerZone";
import { SetupModelManager } from "./SetupModelManager";
import { SetupPlanSection } from "./SetupPlanSection";
import { SetupProgressSection } from "./SetupProgressSection";
import { SetupRecoveryNotice } from "./SetupRecoveryNotice";
import { SetupRiskNotice } from "./SetupRiskNotice";
import { ModelListTip } from "./SetupTips";
import { useSetupModelDownload, type SetupDownloadPhase } from "./useSetupModelDownload";
import { useSetupStatus } from "./useSetupStatus";

export interface SetupPageBridge {
  start: (request: RuntimeSetupRequest) => Promise<RuntimeSetupRunnerStatus>;
  status: () => Promise<RuntimeSetupRunnerStatus>;
  cancel: () => Promise<RuntimeSetupRunnerStatus>;
  restart: () => Promise<void>;
  environment: () => Promise<RuntimeSetupEnvironment>;
  listen: (onProgress: (event: SetupProgressEvent) => void) => Promise<() => void>;
}

const defaultBridge: SetupPageBridge = {
  start: startRuntimeSetup,
  status: getRuntimeSetupRunnerStatus,
  cancel: cancelRuntimeSetup,
  restart: restartOrpheusBackend,
  environment: getRuntimeSetupEnvironment,
  listen: listenRuntimeSetupProgress,
};

interface SetupPageProps {
  apiClient: ApiClient | null;
  initialStatus: RuntimeSetupStatusResponse | null;
  initialError: string | null;
  onReady: (status: RuntimeSetupStatusResponse) => void;
  onContinueLibraryOnly: () => void;
  settingsEntry?: boolean;
  onExitSettings?: () => void;
  onReconnectBackend: () => void;
  bridge?: SetupPageBridge;
}

export function SetupPage({
  apiClient,
  initialStatus,
  initialError,
  onReady,
  onContinueLibraryOnly,
  settingsEntry = false,
  onExitSettings,
  onReconnectBackend,
  bridge = defaultBridge,
}: SetupPageProps) {
  const [progress, setProgress] = useState<SetupProgressState>(EMPTY_PROGRESS);
  const handleReady = useCallback((next: RuntimeSetupStatusResponse) => {
    setProgress((previous) => ({ ...previous, restartRequired: false }));
    onReady(next);
  }, [onReady]);
  const { status, setStatus, selection, setSelection, error, setError, refreshStatus } = useSetupStatus({
    apiClient, initialStatus, initialError, onReady: handleReady, preserveSelection: settingsEntry,
  });
  const [busy, setBusy] = useState(false);
  const [runnerStopping, setRunnerStopping] = useState(false);
  const [copyLabel, setCopyLabel] = useState("Copy diagnostics");
  const [environment, setEnvironment] = useState<RuntimeSetupEnvironment | null>(null);
  const [listenerReady, setListenerReady] = useState(false);
  const [runnerReconciled, setRunnerReconciled] = useState(false);
  const [listenerError, setListenerError] = useState<string | null>(null);
  const [runnerError, setRunnerError] = useState<string | null>(null);
  const [listenerAttempt, setListenerAttempt] = useState(0);
  const [awaitingReconnect, setAwaitingReconnect] = useState(false);
  const [adopting, setAdopting] = useState(false);
  const [managingModels, setManagingModels] = useState(false);
  const [downloadingGroup, setDownloadingGroup] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(settingsEntry);
  const [downloadPhase, setDownloadPhase] = useState<SetupDownloadPhase>("idle");
  const [statusRefreshing, setStatusRefreshing] = useState(false);
  const startLocked = useRef(false);
  const restartLocked = useRef(false);
  const adoptLocked = useRef(false);
  const adoptGeneration = useRef(0);
  const lastAttempt = useRef<"backend" | "bridge" | null>(null);
  const statusRefreshLock = useRef(false);
  const preRestartClient = useRef<ApiClient | null>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const setupHeadingRef = useRef<HTMLHeadingElement>(null);
  const progressHeadingRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    adoptGeneration.current += 1;
    adoptLocked.current = false;
    setAdopting(false);
    setManagingModels(false);
    return () => { adoptGeneration.current += 1; };
  }, [apiClient]);

  const applyRunnerSnapshot = useCallback((runner: RuntimeSetupRunnerStatus) => {
    setProgress((previous) => applyRunnerStatus(previous, runner));
    const running = runner.lifecycle === "running" || runner.lifecycle === "cancelling";
    setRunnerStopping(runner.lifecycle === "cancelling");
    setBusy(running);
    startLocked.current = running;
    return running;
  }, []);

  const refreshRunner = useCallback(async () => {
    if (restartLocked.current) return null;
    try {
      const runner = await bridge.status();
      // A poll may have started before restart. Reconnection owns busy state
      // and readiness until the replacement backend has been checked.
      if (restartLocked.current) return runner;
      const running = applyRunnerSnapshot(runner);
      // The installer has verified the new runtime. The old backend cannot
      // report its readiness until restart loads the installed packages.
      if (!running && runner.lifecycle !== "idle" && !runner.restartRequired) {
        await refreshStatus();
      }
      return runner;
    } catch {
      return null;
    }
  }, [applyRunnerSnapshot, bridge, refreshStatus]);

  const reconcileRunner = useCallback(async () => {
    const runner = await refreshRunner();
    if (restartLocked.current) return runner;
    if (runner) {
      setRunnerReconciled(true);
      setRunnerError(null);
    } else {
      setRunnerReconciled(false);
      setRunnerError("Runner status is unavailable. Retry the progress connection.");
    }
    return runner;
  }, [refreshRunner]);

  const reconcileRunnerRef = useRef(reconcileRunner);
  useEffect(() => { reconcileRunnerRef.current = reconcileRunner; }, [reconcileRunner]);

  useEffect(() => {
    let mounted = true;
    void bridge.environment().then((value) => {
      if (mounted) setEnvironment(value);
    }).catch((reason: unknown) => {
      if (mounted) setEnvironment({ available: false, mode: "browser", reason: safeError(reason, "Runtime installation is unavailable.") });
    });
    return () => { mounted = false; };
  }, [bridge]);

  useEffect(() => {
    if (!environment?.available) {
      setListenerReady(false);
      setRunnerReconciled(false);
      setListenerError(null);
      setRunnerError(null);
      return undefined;
    }
    let mounted = true;
    let release: (() => void) | null = null;
    setListenerReady(false);
    setRunnerReconciled(false);
    setListenerError(null);
    setRunnerError(null);
    void bridge.listen((event) => {
      if (!mounted) return;
      setProgress((previous) => applyProgressEvent(previous, event));
      if (event.terminal) {
        void reconcileRunnerRef.current();
      }
    }).then((unlisten) => {
      if (!mounted) {
        unlisten();
        return;
      }
      release = unlisten;
      setListenerReady(true);
      void reconcileRunnerRef.current();
    }).catch((reason: unknown) => {
      if (mounted) {
        setListenerReady(false);
        setListenerError(safeError(reason, "Progress connection unavailable. Retry the progress connection."));
      }
    });
    return () => {
      mounted = false;
      release?.();
    };
  }, [bridge, environment?.available, listenerAttempt]);

  useSetupModelDownload({
    apiClient,
    environment,
    downloadPhase,
    selection,
    onReady,
    onModelDownloadReady: (next) => {
      if (apiClient) invalidateRuntimeBootstrap(apiClient);
      onReady(next);
    },
    onDownloadingModel: setDownloadingGroup,
    setDownloadPhase,
    setBusy,
    setError,
    setProgress,
    setStatus,
    startLocked,
    lastAttempt,
  });

  useEffect(() => {
    setupHeadingRef.current?.focus();
  }, []);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  useEffect(() => {
    if (busy || progress.lifecycle === "complete" || progress.lifecycle === "failed") {
      progressHeadingRef.current?.focus();
    }
  }, [busy, progress.lifecycle]);

  useEffect(() => {
    if (settingsEntry) {
      setupHeadingRef.current?.focus();
    }
  }, [progress.lifecycle, settingsEntry, status]);

  useEffect(() => {
    if (!environment?.available || !busy || awaitingReconnect || downloadPhase !== "idle") return undefined;
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await refreshRunner();
      if (active) timer = setTimeout(() => void poll(), 1_000);
    };
    timer = setTimeout(() => void poll(), 1_000);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [awaitingReconnect, busy, downloadPhase, environment?.available, refreshRunner]);

  useEffect(() => {
    if (!awaitingReconnect || !apiClient || apiClient === preRestartClient.current) {
      return undefined;
    }
    let active = true;
    void refreshStatus().then((next) => {
      if (!active) return;
      if (next && !next.restart_required && !next.ready) {
        const message = "Orpheus restarted, but the selected runtime is not ready. Install again to repair it.";
        lastAttempt.current = "bridge";
        setError(message);
        setProgress((previous) => ({ ...previous, lifecycle: "failed", restartRequired: false,
          completedStages: previous.completedStages.filter((stage) => stage !== "verify"), message }));
      }
    }).finally(() => {
      if (!active) return;
      preRestartClient.current = null;
      restartLocked.current = false;
      setAwaitingReconnect(false);
      setBusy(false);
    });
    return () => {
      active = false;
    };
  }, [apiClient, awaitingReconnect, refreshStatus]);

  const selectedProfile = status?.profiles.find((profile) => profile.id === selection.profile) ?? null;
  const choices = status ? profileChoices(status) : [];
  const mutationsAvailable = environment?.available === true;
  const installedRuntime = status?.installed_runtime ?? null;
  const installedReady = installedRuntime?.ready === true;
  const configuredReady = status?.configured === true && status.ready && status.state === "ready" && !status.restart_required;
  const installedProfile = installedReady
    ? status?.profiles.find((profile) => profile.id === installedRuntime?.profile) ?? null
    : null;
  const installedIsCuda = installedReady && installedRuntime?.profile === "cuda";
  const installedReadyGroups = installedReady
    ? installedRuntime!.model_groups.filter((group) => group.ready).map((group) => group.id)
    : [];
  const riskProfile = installedReady && !settingsEntry ? installedProfile : selectedProfile;
  const showRiskBlock = installedReady && !settingsEntry
    ? Boolean(installedIsCuda && installedProfile?.risk)
    : selection.profile === "cuda" && Boolean(selectedProfile?.risk);
  const adoptDisabled = busy || adopting || Boolean(installedProfile?.risk && !selection.acceptPytorchRisk);
  const readyToStart = Boolean(listenerReady && runnerReconciled && mutationsAvailable && status && canStartSetup(selection, status) && !busy && !adopting && !startLocked.current);
  const packagedMode = environment !== null && !mutationsAvailable;
  const backendInstallProfile = backendInstallProfileId(mutationsAvailable, installedRuntime);
  const selectedReadiness = status ? selectionReadiness(selection, status) : { runtimeReady: false, ready: false };
  const backendReadyToStart = Boolean(
    apiClient
    && status
    && canStartSetup(selection, status)
    && downloadPhase === "idle"
    && !busy
    && !adopting
    && !startLocked.current,
  );
  const showRestart = awaitingReconnect || Boolean(status?.restart_required) || progress.restartRequired;
  const progressValue = progress.percent === null ? undefined : progress.percent;

  const cachedGroups = installedRuntime?.profile === selection.profile ? installedRuntime.model_groups
    : status?.selected_profile === selection.profile ? status.selected_model_groups : [];
  const missingGroups = selection.modelGroups.filter((id) => !cachedGroups.some((group) => group.id === id && group.assets_ready));
  const selectedEstimate = missingGroups.length ? selectionEstimate(missingGroups) : "Already downloaded · no download needed";
  const progressMessage = showRestart && (progress.lifecycle === "idle" || progress.lifecycle === "complete") && !awaitingReconnect
    ? "Installation complete. Restart Orpheus to load the selected runtime."
    : progress.message;

  const start = async (): Promise<boolean> => {
    if (!mutationsAvailable || !status || !canStartSetup(selection, status) || !acquireSetupStart(startLocked)) return false;
    lastAttempt.current = "bridge";
    setBusy(true);
    setError(null);
    setProgress({ ...EMPTY_PROGRESS, lifecycle: "running", message: "Preparing the selected runtime…" });
    try {
      const runner = await bridge.start({
        profile: selection.profile,
        modelGroups: [...selection.modelGroups],
        cleanerCudaQuantization: selection.cleanerCudaQuantization ?? "4bit",
        acceptPytorchRisk: selection.acceptPytorchRisk,
      });
      const running = applyRunnerSnapshot(runner);
      if (!running && !runner.restartRequired) await refreshStatus();
    } catch (reason: unknown) {
      startLocked.current = false;
      setBusy(false);
      const message = safeError(reason, "Runtime setup could not start.");
      setError(message);
      setProgress((previous) => ({ ...previous, lifecycle: "failed", message }));
    }
    return true;
  };

  const installFromBackend = async (): Promise<boolean> => {
    if (!apiClient || !status || selection.profile !== backendInstallProfile || !selectedReadiness.runtimeReady || !acquireSetupStart(startLocked)) return false;
    lastAttempt.current = "backend";
    setDownloadPhase("starting");
    setBusy(true);
    setError(null);
    setProgress({ ...EMPTY_PROGRESS, lifecycle: "running", message: "Starting model downloads…" });
    try {
      await apiClient.startRuntimeModelDownload(
        { profile: selection.profile, model_groups: [...selection.modelGroups], cleaner_cuda_quantization: selection.cleanerCudaQuantization ?? "4bit" },
        { timeoutMs: 15_000 },
      );
      setDownloadPhase("downloading");
    } catch (reason: unknown) {
      startLocked.current = false;
      setBusy(false);
      const message = safeError(reason, "Model downloads could not start.");
      setDownloadPhase("idle");
      setError(message);
      setProgress((previous) => ({ ...previous, lifecycle: "failed", message }));
    }
    return true;
  };

  const installModel = async (profile: "base" | "mlx" | "cuda", groupId: string) => {
    if (!apiClient || busy || adopting || managingModels || !acquireSetupStart(startLocked)) return;
    lastAttempt.current = null;
    setDownloadPhase("starting");
    setBusy(true);
    setError(null);
    setDownloadingGroup(groupId);
    setProgress({ ...EMPTY_PROGRESS, lifecycle: "running", message: "Starting model download…" });
    try {
      await apiClient.startRuntimeModelDownload({ profile, model_groups: [groupId], configure_runtime: false }, { timeoutMs: 15_000 });
      setDownloadPhase("downloading");
    } catch (reason: unknown) {
      startLocked.current = false;
      setBusy(false);
      setError(safeError(reason, "The model download could not start. Try again."));
      setDownloadPhase("idle");
      setProgress(EMPTY_PROGRESS);
    }
  };

  const modelDeleted = async () => {
    const next = await refreshStatus();
    if (!next) throw new Error("The model was deleted, but its status could not be refreshed. Check runtime to retry.");
    setSelection(createSelection(next));
    setProgress(EMPTY_PROGRESS);
    if (apiClient) invalidateRuntimeBootstrap(apiClient);
    onReady(next);
  };

  // Keep the progress layout in place while retrying so controls do not shift mid-click.
  const retryFailedAttempt = async () => {
    // A second click while the first retry is in flight must not clobber the
    // freshly-started progress state.
    if (startLocked.current) return;
    setError(null);
    const retried = lastAttempt.current === "backend"
      ? await installFromBackend()
      : lastAttempt.current === "bridge"
        ? await start()
        : false;
    if (!retried) {
      setProgress(EMPTY_PROGRESS);
    }
  };

  const retryStatus = async () => {
    // Ref guard: two clicks in the same tick both see the stale state value.
    if (statusRefreshLock.current) return;
    statusRefreshLock.current = true;
    setStatusRefreshing(true);
    try {
      await refreshStatus();
    } finally {
      statusRefreshLock.current = false;
      setStatusRefreshing(false);
    }
  };

  const cancel = async () => {
    if (!busy) return;
    if (downloadPhase === "downloading" && apiClient) {
      try {
        const snapshot = await apiClient.cancelRuntimeModelDownload({ timeoutMs: 8_000 });
        if (snapshot.lifecycle === "stopping") setDownloadPhase("stopping");
        setProgress((previous) => applyProgressEvent(previous, modelDownloadProgressEvent(snapshot)));
        if (snapshot.lifecycle === "cancelled") {
          setDownloadPhase("idle");
          startLocked.current = false;
          setBusy(false);
          setProgress({ ...EMPTY_PROGRESS, message: "Model download cancelled." });
        }
      } catch (reason: unknown) {
        setError(safeError(reason, "Model download could not be cancelled."));
      }
      return;
    }
    if (!mutationsAvailable) return;
    try {
      const runner = await bridge.cancel();
      applyRunnerSnapshot(runner);
    } catch (reason: unknown) {
      setError(safeError(reason, "Setup could not be cancelled."));
    }
  };

  const restart = async () => {
    if (busy || !mutationsAvailable || restartLocked.current) return;
    restartLocked.current = true;
    preRestartClient.current = apiClient;
    setAwaitingReconnect(true);
    setBusy(true);
    setError(null);
    try {
      await restartAndReconnectSetup(bridge.restart, onReconnectBackend);
      // Restart authorization is consumed even if runtime verification fails.
      // Never offer the consumed restart again while checking the new backend.
      setStatus((previous) => previous ? { ...previous, restart_required: false } : previous);
      setProgress((previous) => ({ ...previous, restartRequired: false, message: "Orpheus restarted. Checking the runtime…" }));
    } catch (reason: unknown) {
      preRestartClient.current = null;
      restartLocked.current = false;
      setAwaitingReconnect(false);
      setBusy(false);
      setError(safeError(reason, "Orpheus could not restart."));
    }
  };

  const copyDiagnostics = async () => {
    try {
      await navigator.clipboard.writeText(diagnosticsSnapshot(status, progress));
      setCopyLabel("Copied");
    } catch {
      setCopyLabel("Copy unavailable");
    }
  };

  const adoptInstalled = async () => {
    if (!apiClient || !installedReady || !installedRuntime || adoptDisabled || !acquireSetupStart(adoptLocked)) return;
    const generation = adoptGeneration.current;
    setAdopting(true);
    setError(null);
    try {
      const next = await apiClient.adoptRuntimeSetup(
        {
          profile: selection.profile,
          model_groups: [...selection.modelGroups],
          cleaner_cuda_quantization: selection.cleanerCudaQuantization ?? "4bit",
          accept_pytorch_risk: Boolean(installedIsCuda && selection.acceptPytorchRisk),
        },
        { timeoutMs: 120_000 },
      );
      if (generation !== adoptGeneration.current) return;
      setStatus(next);
      if (next.ready && next.state === "ready" && !next.restart_required) onReady(next);
      else setError("The installed runtime is not ready. Check its status and try again.");
    } catch (reason: unknown) {
      if (generation === adoptGeneration.current) setError(safeError(reason, "The installed runtime could not be adopted."));
    } finally {
      if (generation === adoptGeneration.current) {
        adoptLocked.current = false;
        setAdopting(false);
      }
    }
  };

  const saveSelection = async () => {
    if (!apiClient || !selectedReadiness.ready || busy || !acquireSetupStart(adoptLocked)) return;
    const generation = adoptGeneration.current;
    setAdopting(true);
    setError(null);
    try {
      const next = await apiClient.adoptRuntimeSetup({
        profile: selection.profile,
        model_groups: [...selection.modelGroups],
        accept_pytorch_risk: selection.acceptPytorchRisk,
        cleaner_cuda_quantization: selection.cleanerCudaQuantization ?? "4bit",
      }, { timeoutMs: 120_000 });
      if (generation !== adoptGeneration.current) return;
      setStatus(next);
      if (next.ready && next.state === "ready" && !next.restart_required) {
        onReady(next);
      } else {
        setError("The selected runtime is not ready. Check its status and try again.");
      }
    } catch (reason: unknown) {
      if (generation === adoptGeneration.current) setError(safeError(reason, "Runtime settings could not be saved. Try again."));
    } finally {
      if (generation === adoptGeneration.current) {
        adoptLocked.current = false;
        setAdopting(false);
      }
    }
  };

  const continueLibraryOnly = async () => {
    if (!mutationsAvailable && apiClient) {
      try {
        const next = await apiClient.adoptRuntimeSetup(
          { profile: "base", model_groups: [], accept_pytorch_risk: false },
          { timeoutMs: 15_000 },
        );
        setStatus(next);
        if (next.ready && next.state === "ready" && !next.restart_required) {
          onReady(next);
          return;
        }
        // Adopt persisted the choice but this environment still is not
        // operational; fall through to the session override below.
      } catch {
        // Fall back to the in-memory override so the user is never trapped.
      }
    }
    onContinueLibraryOnly();
  };

  const resolvePrimaryAction = (): { label: string; disabled: boolean; run: () => void | Promise<unknown>; disabledReason: string | null } => {
    if (adopting) return { label: settingsEntry ? "Saving…" : "Continue…", disabled: true, disabledReason: null, run: () => undefined };
    if (managingModels) return { label: "Updating models…", disabled: true, disabledReason: null, run: () => undefined };
    if (selection.profile !== "base" && !selection.modelGroups.some((id) => MODEL_GROUP_DETAILS[id]?.kind === "tts")) {
      return { label: "Continue", disabled: true, disabledReason: "Select the Kokoro voice model — it's required to create audio", run: () => undefined };
    }
    if (configuredReady && status && selectionMatchesConfigured(selection, status) && !showRestart) {
      return { label: "Done", disabled: busy || adopting, disabledReason: null, run: () => onExitSettings ? onExitSettings() : onReady(status) };
    }
    if (showRestart) {
      const disabled = busy || !mutationsAvailable;
      return {
        label: "Restart Orpheus",
        disabled,
        disabledReason: disabled && !mutationsAvailable ? "Restart is available in the Orpheus desktop app." : null,
        run: restart,
      };
    }
    if (settingsEntry && selectedReadiness.ready) {
      return { label: "Save runtime settings", disabled: !apiClient || busy, disabledReason: null, run: saveSelection };
    }
    if (selection.profile !== "base" && selection.modelGroups.length > 0 && selection.modelGroups.every((id) => cachedGroups.some((group) => group.id === id && group.ready)) && !settingsEntry) {
      return {
        label: "Continue",
        disabled: adoptDisabled,
        disabledReason: adoptDisabled && installedProfile?.risk && !selection.acceptPytorchRisk
          ? "Accept the security notice to continue."
          : null,
        run: adoptInstalled,
      };
    }
    if (packagedMode && selection.profile === "base") {
      return { label: "Use library only", disabled: busy || adopting, disabledReason: null, run: continueLibraryOnly };
    }
    if (packagedMode && backendInstallProfile !== null && selection.profile === backendInstallProfile && selectedReadiness.runtimeReady) {
      return {
        label: "Install",
        disabled: !backendReadyToStart,
        disabledReason: !backendReadyToStart && !busy
          ? (!canStartSetup(selection, status!) ? "Choose a compatible runtime and accept any required notices." : null)
          : null,
        run: installFromBackend,
      };
    }
    const label = selection.profile === "base" ? "Use library only" : "Install";
    if (readyToStart) {
      return { label, disabled: false, disabledReason: null, run: start };
    }
    let disabledReason: string | null = null;
    if (status && !canStartSetup(selection, status)) {
      disabledReason = "Choose a compatible runtime, select at least one model, and accept any required notices.";
    } else if (!mutationsAvailable) {
      disabledReason = selection.profile === "base"
        ? null
        : environment?.mode === "packaged"
          ? "This runtime is unavailable in this installation. Check its status or install an Orpheus build with this runtime."
          : "Open the desktop app to install this runtime.";
    } else if (!listenerReady || !runnerReconciled) {
      disabledReason = "Connecting to the installer…";
    }
    return { label, disabled: true, disabledReason, run: start };
  };
  const action = resolvePrimaryAction();
  const showEnvironmentNotice = Boolean(
    status && environment && !mutationsAvailable && !installedReady && !configuredReady
    && !backendInstallProfile && !showRestart && !busy,
  );
  const configuredRuntimeLabel = status?.selected_profile === "base"
    ? "Library-only mode is ready"
    : `${status?.profiles.find((profile) => profile.id === status.selected_profile)?.label ?? "Local"} runtime is ready`;

  return (
    <main className="setup-frame" aria-label="Orpheus runtime setup">
      <div className="setup-shell">
        <section className="setup-panel" aria-labelledby="setup-heading">
          <div className="setup-intro">
            {settingsEntry && onExitSettings ? (
              <button id="setup-back-btn" className="btn btn-ghost setup-back" type="button" disabled={(busy && downloadPhase !== "stopping" && !runnerStopping) || adopting || managingModels} onClick={onExitSettings}>
                Back to dashboard
              </button>
            ) : null}
            <h1 id="setup-heading" ref={setupHeadingRef} tabIndex={-1}>{settingsEntry ? "Runtime setup" : "Welcome to Orpheus"}</h1>
            <p className="setup-lede">{settingsEntry ? "Manage the runtime and models on this device." : "Choose how Orpheus creates audiobooks on this device."}</p>
          </div>

          {configuredReady && status ? (
            <div className="setup-installed" role="status">
              <strong>{configuredRuntimeLabel}</strong>
              <ModelListTip modelGroups={status.selected_model_groups.filter((group) => group.ready).map((group) => group.id)} label="Configured runtime details" />
            </div>
          ) : null}

          {installedReady && installedRuntime && !configuredReady ? (
            <div className="setup-installed" role="note">
              <strong>Installed {RUNTIME_LABELS[installedRuntime.profile]} runtime ready</strong>
              <ModelListTip modelGroups={installedReadyGroups} label="Installed runtime details" />
            </div>
          ) : null}

          {!status && !error ? (
            <div className="setup-loading" role="status">
              <span className="setup-loading-spinner" aria-hidden="true" />
              <span>Checking this device…</span>
            </div>
          ) : null}

          {!status && error ? (
            <SetupRecoveryNotice title="Setup status unavailable" message={error}>
              <div className="setup-actions">
                <button className="btn" type="button" disabled={statusRefreshing} onClick={() => void retryStatus()}>
                  {statusRefreshing ? "Checking…" : "Retry"}
                </button>
                <button className="btn" type="button" disabled={statusRefreshing} onClick={() => void continueLibraryOnly()}>
                  Use library only
                </button>
              </div>
            </SetupRecoveryNotice>
          ) : null}

          {showEnvironmentNotice ? (
            <SetupRecoveryNotice
              environment
              title={environment?.mode === "packaged" ? "Audio creation is unavailable" : "Open the desktop app to set up audio creation"}
              message={environment?.mode === "packaged"
                ? "Orpheus could not verify the installed runtime. Check again, or reinstall Orpheus if the problem continues. You can still use your library."
                : "Runtime installation is available in the Orpheus desktop app. You can still use your library here."}
            >
              <div className="setup-actions">
                <button className="btn" type="button" disabled={statusRefreshing} onClick={() => void retryStatus()}>
                  {statusRefreshing ? "Checking…" : "Check again"}
                </button>
                <button className="btn" type="button" onClick={() => void continueLibraryOnly()}>Use library only</button>
              </div>
            </SetupRecoveryNotice>
          ) : null}

          {listenerError ? (
            <SetupRecoveryNotice title="Progress connection unavailable" message={listenerError}>
              <div className="setup-actions">
                <button className="btn" type="button" onClick={() => {
                  setListenerError(null);
                  setListenerAttempt((attempt) => attempt + 1);
                }}>
                  Retry progress connection
                </button>
                <button className="btn" type="button" onClick={() => void continueLibraryOnly()}>Continue in library-only mode</button>
              </div>
            </SetupRecoveryNotice>
          ) : null}

          {runnerError && !listenerError ? (
            <SetupRecoveryNotice title="Runner status unavailable" message={runnerError}>
              <button className="btn" type="button" onClick={() => {
                setRunnerError(null);
                void reconcileRunner();
              }}>
                Retry runner status
              </button>
            </SetupRecoveryNotice>
          ) : null}

          {status ? (
            <SetupPlanSection
              status={status}
              selection={selection}
              setSelection={setSelection}
              selectedProfile={selectedProfile}
              selectedEstimate={selectedEstimate}
              choices={choices}
              busy={busy || managingModels}
              adopting={adopting}
              showAdvanced={showAdvanced}
              setShowAdvanced={setShowAdvanced}
              showRiskBlock={showRiskBlock}
              riskProfile={riskProfile}
              showModels={!settingsEntry}
            />
          ) : null}

          {settingsEntry && status ? <SetupModelManager
            apiClient={apiClient} status={status} selection={selection} setSelection={setSelection}
            disabled={busy || adopting || managingModels || showRestart}
            downloadingGroup={busy ? downloadingGroup : null}
            downloadMessage={busy ? progressMessage : null}
            onInstall={installModel} onDeleted={modelDeleted} onBusyChange={setManagingModels}
          /> : null}

          {status && installedReady && !settingsEntry && showRiskBlock && riskProfile?.risk ? (
            <SetupRiskNotice
              summary={riskProfile.risk.summary}
              checked={selection.acceptPytorchRisk}
              disabled={busy || adopting}
              onChange={(event) => setSelection((current) => ({ ...current, acceptPytorchRisk: event.target.checked }))}
            />
          ) : null}

          {(busy || progress.lifecycle !== "idle" || showRestart) ? (
            <SetupProgressSection
              progress={progress}
              progressMessage={progressMessage}
              progressValue={progressValue}
              modelGroups={selection.modelGroups}
              headingRef={progressHeadingRef}
            />
          ) : null}

          {error && status ? (
            <div className="setup-error" role="alert" ref={errorRef} tabIndex={-1}>
              <strong>Something went wrong</strong>
              <p>{error}</p>
              {!busy && progress.lifecycle !== "failed" && !showRestart ? <button className="btn" type="button" disabled={statusRefreshing} onClick={() => void retryStatus()}>{statusRefreshing ? "Checking…" : "Retry status check"}</button> : null}
            </div>
          ) : null}

          {status?.state_error && !["invalid", "unavailable"].includes(status.state_error) ? <p className="setup-model-feedback is-error" role="alert">{status.state_error}</p> : null}

          {settingsEntry && !busy && !adopting && !managingModels ? (
            <SetupDangerZone
              apiClient={apiClient}
              onDeleted={refreshStatus}
              onError={setError}
            />
          ) : null}

          <footer className="setup-footer">
            <div className="setup-footer-left">
              {settingsEntry && status ? <button className="btn btn-ghost" type="button" disabled={busy || adopting || managingModels || statusRefreshing} onClick={() => void retryStatus()}>{statusRefreshing ? "Checking…" : "Check runtime"}</button> : null}
              {(error || listenerError || runnerError || progress.lifecycle === "failed") ? <button className="btn btn-ghost" type="button" onClick={() => void copyDiagnostics()}>{copyLabel}</button> : null}
              {action.disabled && action.disabledReason ? (
                <span className="hint setup-action-hint">{action.disabledReason}</span>
              ) : null}
            </div>
            <div className="setup-actions">
              {!settingsEntry && (downloadPhase === "stopping" || runnerStopping) ? <button className="btn btn-ghost" type="button" onClick={onContinueLibraryOnly}>Back to dashboard</button> : null}
              {busy && downloadPhase !== "starting" && downloadPhase !== "adopting" && (mutationsAvailable || downloadPhase === "downloading") ? <button className="btn" type="button" onClick={() => void cancel()}>Cancel</button> : null}
              {progress.lifecycle === "failed" && !busy ? <button className="btn" type="button" onClick={() => void retryFailedAttempt()}>Retry</button> : null}
              <button className="btn btn-primary btn-lg setup-primary" type="button" disabled={action.disabled} aria-busy={adopting} onClick={() => void action.run()}>
                {adopting ? <BrailleSpinner active /> : null}
                {action.label}
              </button>
            </div>
          </footer>
        </section>
      </div>
    </main>
  );
}

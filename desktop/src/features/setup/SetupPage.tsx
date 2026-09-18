import { useCallback, useEffect, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import type { RuntimeSetupStatusResponse } from "../../types/api";
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
  diagnosticsSnapshot,
  modelDownloadProgressEvent,
  profileChoices,
  selectionEstimate,
  type SetupProgressEvent,
  type SetupProgressState,
} from "./setupModel";
import { restartAndReconnectSetup } from "./setupLifecycle";
import {
  EMPTY_PROGRESS,
  INSTALLED_PROFILE_LABEL,
  safeError,
} from "./setupPageHelpers";
import { SetupDangerZone } from "./SetupDangerZone";
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
    apiClient, initialStatus, initialError, onReady: handleReady,
  });
  const [busy, setBusy] = useState(false);
  const [copyLabel, setCopyLabel] = useState("Copy diagnostics");
  const [environment, setEnvironment] = useState<RuntimeSetupEnvironment | null>(null);
  const [listenerReady, setListenerReady] = useState(false);
  const [runnerReconciled, setRunnerReconciled] = useState(false);
  const [listenerError, setListenerError] = useState<string | null>(null);
  const [runnerError, setRunnerError] = useState<string | null>(null);
  const [listenerAttempt, setListenerAttempt] = useState(0);
  const [awaitingReconnect, setAwaitingReconnect] = useState(false);
  const [adopting, setAdopting] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [downloadPhase, setDownloadPhase] = useState<SetupDownloadPhase>("idle");
  const [statusRefreshing, setStatusRefreshing] = useState(false);
  const startLocked = useRef(false);
  const restartLocked = useRef(false);
  const lastAttempt = useRef<"backend" | "bridge" | null>(null);
  const statusRefreshLock = useRef(false);
  const preRestartClient = useRef<ApiClient | null>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const setupHeadingRef = useRef<HTMLHeadingElement>(null);
  const progressHeadingRef = useRef<HTMLDivElement>(null);

  const applyRunnerSnapshot = useCallback((runner: RuntimeSetupRunnerStatus) => {
    setProgress((previous) => applyRunnerStatus(previous, runner));
    const running = runner.lifecycle === "running" || runner.lifecycle === "cancelling";
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
    if (!environment?.available || !busy || awaitingReconnect) return undefined;
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
  }, [awaitingReconnect, busy, environment?.available, refreshRunner]);

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
  const riskProfile = installedReady ? installedProfile : selectedProfile;
  const showRiskBlock = installedReady
    ? Boolean(installedIsCuda && installedProfile?.risk)
    : selection.profile === "cuda" && Boolean(selectedProfile?.risk);
  const adoptDisabled = adopting || Boolean(installedProfile?.risk && !selection.acceptPytorchRisk);
  const readyToStart = Boolean(listenerReady && runnerReconciled && mutationsAvailable && status && canStartSetup(selection, status) && !busy && !startLocked.current);
  const packagedMode = environment !== null && !mutationsAvailable;
  const backendInstallProfile = backendInstallProfileId(mutationsAvailable, installedRuntime);
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

  const selectedEstimate = selectionEstimate(selection.modelGroups);
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
    if (!apiClient || !status || selection.profile !== backendInstallProfile || !canStartSetup(selection, status) || !acquireSetupStart(startLocked)) return false;
    lastAttempt.current = "backend";
    setBusy(true);
    setError(null);
    setProgress({ ...EMPTY_PROGRESS, lifecycle: "running", message: "Starting model downloads…" });
    try {
      await apiClient.startRuntimeModelDownload(
        { profile: selection.profile, model_groups: [...selection.modelGroups] },
        { timeoutMs: 15_000 },
      );
      setDownloadPhase("downloading");
    } catch (reason: unknown) {
      startLocked.current = false;
      setBusy(false);
      const message = safeError(reason, "Model downloads could not start.");
      setError(message);
      setProgress((previous) => ({ ...previous, lifecycle: "failed", message }));
    }
    return true;
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
    if (packagedMode && downloadPhase === "downloading" && apiClient) {
      try {
        const snapshot = await apiClient.cancelRuntimeModelDownload({ timeoutMs: 8_000 });
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
    if (!apiClient || !installedReady || !installedRuntime || adoptDisabled) return;
    setAdopting(true);
    setError(null);
    try {
      const next = await apiClient.adoptRuntimeSetup(
        {
          profile: installedRuntime.profile,
          model_groups: [...installedReadyGroups],
          accept_pytorch_risk: Boolean(installedIsCuda && selection.acceptPytorchRisk),
        },
        { timeoutMs: 15_000 },
      );
      setStatus(next);
      onReady(next);
    } catch (reason: unknown) {
      setError(safeError(reason, "The installed runtime could not be adopted."));
    } finally {
      setAdopting(false);
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

  const resolvePrimaryAction = () => {
    if (configuredReady && status) {
      return { label: "Done", disabled: false, run: () => onExitSettings ? onExitSettings() : onReady(status) };
    }
    if (showRestart) {
      return { label: "Restart Orpheus", disabled: busy || !mutationsAvailable, run: restart };
    }
    if (installedReady) {
      return { label: "Continue", disabled: adoptDisabled, run: adoptInstalled };
    }
    if (packagedMode && selection.profile === "base") {
      return { label: "Use library only", disabled: busy || adopting, run: continueLibraryOnly };
    }
    if (packagedMode && backendInstallProfile !== null && selection.profile === backendInstallProfile) {
      return { label: "Install", disabled: !backendReadyToStart, run: installFromBackend };
    }
    return { label: selection.profile === "base" ? "Use library only" : "Install", disabled: !readyToStart, run: start };
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
              <button id="setup-back-btn" className="btn btn-ghost btn-compact setup-back" type="button" onClick={onExitSettings}>
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

          {installedReady && installedRuntime ? (
            <div className="setup-installed" role="note">
              <strong>Installed {INSTALLED_PROFILE_LABEL[installedRuntime.profile]} runtime ready</strong>
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

          {status && !installedReady && !configuredReady ? (
            <SetupPlanSection
              status={status}
              selection={selection}
              setSelection={setSelection}
              selectedProfile={selectedProfile}
              selectedEstimate={selectedEstimate}
              choices={choices}
              busy={busy}
              adopting={adopting}
              showAdvanced={showAdvanced}
              setShowAdvanced={setShowAdvanced}
              showRiskBlock={showRiskBlock}
              riskProfile={riskProfile}
            />
          ) : null}

          {status && installedReady && showRiskBlock && riskProfile?.risk ? (
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

          {settingsEntry && !busy ? (
            <SetupDangerZone
              apiClient={apiClient}
              onDeleted={refreshStatus}
              onError={setError}
            />
          ) : null}

          <footer className="setup-footer">
            <div className="setup-footer-left">
              {(error || listenerError || runnerError || progress.lifecycle === "failed") ? <button className="btn btn-ghost btn-compact" type="button" onClick={() => void copyDiagnostics()}>{copyLabel}</button> : null}
            </div>
            <div className="setup-actions">
              {busy && (mutationsAvailable || (packagedMode && downloadPhase === "downloading")) ? <button className="btn" type="button" onClick={() => void cancel()}>Cancel</button> : null}
              {progress.lifecycle === "failed" && !busy ? <button className="btn" type="button" onClick={() => void retryFailedAttempt()}>Retry</button> : null}
              <button className="btn btn-primary setup-primary" type="button" disabled={action.disabled} onClick={() => void action.run()}>
                {action.label}
              </button>
            </div>
          </footer>
        </section>
      </div>
    </main>
  );
}

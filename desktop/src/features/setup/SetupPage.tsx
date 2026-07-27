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
  createSelection,
  diagnosticsSnapshot,
  modelDownloadProgressEvent,
  profileChoices,
  selectionEstimate,
  type SetupProgressEvent,
  type SetupProgressState,
  type SetupSelection,
} from "./setupModel";
import { restartAndReconnectSetup } from "./setupLifecycle";
import {
  EMPTY_PROGRESS,
  formatFreedBytes,
  INSTALLED_PROFILE_LABEL,
  safeError,
} from "./setupPageHelpers";
import { SetupDangerZone } from "./SetupDangerZone";
import { SetupPlanSection } from "./SetupPlanSection";
import { SetupProgressSection } from "./SetupProgressSection";
import { SetupRiskNotice } from "./SetupRiskNotice";
import { InfoTip, ModelListTip } from "./SetupTips";
import { useSetupModelDownload } from "./useSetupModelDownload";

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
  const [status, setStatus] = useState(initialStatus);
  const [selection, setSelection] = useState<SetupSelection>(() => initialStatus ? createSelection(initialStatus) : { profile: "base", modelGroups: [], acceptPytorchRisk: false });
  const [progress, setProgress] = useState<SetupProgressState>(EMPTY_PROGRESS);
  const [error, setError] = useState(initialError);
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
  const [downloadPhase, setDownloadPhase] = useState<"idle" | "downloading" | "adopting">("idle");
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteResult, setDeleteResult] = useState<string | null>(null);
  const [statusRefreshing, setStatusRefreshing] = useState(false);
  const startLocked = useRef(false);
  const restartLocked = useRef(false);
  const lastAttempt = useRef<"backend" | "bridge" | null>(null);
  const statusRefreshLock = useRef(false);
  const statusRef = useRef(status);
  const deleteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const preRestartClient = useRef<ApiClient | null>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const setupHeadingRef = useRef<HTMLHeadingElement>(null);
  const progressHeadingRef = useRef<HTMLDivElement>(null);

  const refreshStatus = useCallback(async () => {
    if (!apiClient) {
      setError("Runtime setup status is temporarily unavailable.");
      return null;
    }
    try {
      // Matches the app-level fetch: cold probe + first asset hashing can
      // exceed 8s, and a Retry that times out again traps the user.
      const next = await apiClient.getRuntimeSetupStatus({ timeoutMs: 30_000 });
      // Recovering from a failed initial load must also derive the selection,
      // or the recommended profile stays stuck on the base fallback.
      if (!statusRef.current) {
        setSelection(createSelection(next));
      }
      setStatus(next);
      setError(null);
      if (next.ready && next.state === "ready" && !next.restart_required) {
        onReady(next);
      }
      return next;
    } catch (reason: unknown) {
      setError(safeError(reason, "Runtime setup status is temporarily unavailable."));
      return null;
    }
  }, [apiClient, onReady]);

  const refreshRunner = useCallback(async () => {
    try {
      const runner = await bridge.status();
      setProgress((previous) => applyRunnerStatus(previous, runner));
      const running = runner.lifecycle === "running" || runner.lifecycle === "cancelling";
      setBusy(running);
      startLocked.current = running;
      if (!running && runner.lifecycle !== "idle") {
        await refreshStatus();
      }
      return runner;
    } catch {
      return null;
    }
  }, [bridge, refreshStatus]);

  const reconcileRunner = useCallback(async () => {
    const runner = await refreshRunner();
    if (runner) {
      setRunnerReconciled(true);
      setRunnerError(null);
    } else {
      setRunnerReconciled(false);
      setRunnerError("Runner status is unavailable. Retry the progress connection.");
    }
    return runner;
  }, [refreshRunner]);

  useEffect(() => {
    setStatus(initialStatus);
    setError(initialError);
    if (initialStatus) {
      setSelection(createSelection(initialStatus));
    }
  }, [initialError, initialStatus]);

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

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
        void reconcileRunner();
      }
    }).then((unlisten) => {
      if (!mounted) {
        unlisten();
        return;
      }
      release = unlisten;
      setListenerReady(true);
      void reconcileRunner();
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
  }, [bridge, environment?.available, listenerAttempt, reconcileRunner]);

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
    if (!busy || awaitingReconnect) return undefined;
    const timer = window.setInterval(() => void refreshRunner(), 1_000);
    return () => window.clearInterval(timer);
  }, [awaitingReconnect, busy, refreshRunner]);

  useEffect(() => {
    if (!awaitingReconnect || !apiClient || apiClient === preRestartClient.current) {
      return undefined;
    }
    let active = true;
    void refreshStatus().finally(() => {
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
  const showRestart = status?.restart_required || progress.restartRequired;
  const progressValue = progress.percent === null ? undefined : progress.percent;

  const selectedEstimate = selectionEstimate(selection.modelGroups);
  const progressMessage = showRestart && progress.lifecycle === "idle"
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
      setProgress((previous) => applyRunnerStatus(previous, runner));
      const running = runner.lifecycle === "running" || runner.lifecycle === "cancelling";
      setBusy(running);
      startLocked.current = running;
      if (!running) await refreshStatus();
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

  // A failed attempt must actually re-run on Retry. Resetting to the plan view
  // both skips the retry and collapses the layout mid-click, letting a second
  // click land on whichever button shifts under the cursor.
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
      setProgress((previous) => applyRunnerStatus(previous, runner));
      const running = runner.lifecycle === "running" || runner.lifecycle === "cancelling";
      setBusy(running);
      startLocked.current = running;
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
      setProgress((previous) => ({ ...previous, message: "Orpheus restarted. Checking the runtime…" }));
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

  const disarmDelete = useCallback(() => {
    if (deleteTimer.current) {
      clearTimeout(deleteTimer.current);
      deleteTimer.current = null;
    }
    setDeleteArmed(false);
  }, []);

  const runDelete = async () => {
    if (!apiClient) return;
    setDeletePending(true);
    setError(null);
    setDeleteResult(null);
    try {
      const result = await apiClient.deleteRuntimeSetupData({ timeoutMs: 20_000 });
      setDeleteResult(`Freed ${formatFreedBytes(result.freed_bytes)}.`);
      await refreshStatus();
    } catch (reason: unknown) {
      setError(safeError(reason, "Local runtime data could not be deleted."));
    } finally {
      setDeletePending(false);
    }
  };

  const onDeleteClick = () => {
    if (deletePending) return;
    if (!deleteArmed) {
      setDeleteArmed(true);
      deleteTimer.current = setTimeout(() => setDeleteArmed(false), 5_000);
      return;
    }
    disarmDelete();
    void runDelete();
  };

  useEffect(() => () => {
    if (deleteTimer.current) clearTimeout(deleteTimer.current);
  }, []);

  return (
    <main className="setup-frame" aria-label="Orpheus runtime setup">
      <div className="setup-shell">
        <header className="setup-brand">
          <div className="brand"><span className="brand-mark" aria-hidden="true">O</span><span className="brand-name">Orpheus</span></div>
          <span className={`status-dot${apiClient ? " is-connected" : ""}`} aria-hidden="true" />
        </header>

        <section className="setup-panel" aria-labelledby="setup-heading">
          <div className="setup-intro">
            <h1 id="setup-heading" ref={setupHeadingRef} tabIndex={-1}>Welcome to Orpheus</h1>
            <p className="setup-lede">Private, on-device audiobooks. You can change everything later in Settings.</p>
          </div>

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
            <div className="setup-recovery" role="alert">
              <div><strong>Setup status unavailable</strong><p>{error}</p></div>
              <div className="setup-actions"><button className="btn" type="button" disabled={statusRefreshing} onClick={() => void retryStatus()}>{statusRefreshing ? "Checking…" : "Retry"}</button><button className="btn" type="button" disabled={statusRefreshing} onClick={() => void continueLibraryOnly()}>Use library only</button></div>
            </div>
          ) : null}

          {status && environment && !environment.available && !installedReady && !backendInstallProfile ? (
            <div className="setup-recovery setup-environment" role="note">
              <div>
                <strong>Local runtime needs a source checkout <InfoTip label="Why installation is unavailable"><span>{environment.reason}</span></InfoTip></strong>
                <p>Install it from your project checkout by running this command in the project root:</p>
                <div className="setup-command"><code>.venv/bin/python scripts/setup_runtime.py --profile base</code></div>
                <p className="setup-command-note">On Windows, run <code>.venv\Scripts\python.exe</code> instead.</p>
              </div>
            </div>
          ) : null}

          {listenerError ? (
            <div className="setup-recovery" role="alert">
              <div><strong>Progress connection unavailable</strong><p>{listenerError}</p></div>
              <div className="setup-actions"><button className="btn" type="button" onClick={() => { setListenerError(null); setListenerAttempt((attempt) => attempt + 1); }}>Retry progress connection</button><button className="btn" type="button" onClick={() => void continueLibraryOnly()}>Continue in library-only mode</button></div>
            </div>
          ) : null}

          {runnerError && !listenerError ? (
            <div className="setup-recovery" role="alert">
              <div><strong>Runner status unavailable</strong><p>{runnerError}</p></div>
              <button className="btn" type="button" onClick={() => { setRunnerError(null); void reconcileRunner(); }}>Retry runner status</button>
            </div>
          ) : null}

          {status && !installedReady ? (
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
            </div>
          ) : null}

          {settingsEntry && !busy ? (
            <SetupDangerZone
              deletePending={deletePending}
              deleteArmed={deleteArmed}
              deleteResult={deleteResult}
              onDeleteClick={onDeleteClick}
              disarmDelete={disarmDelete}
            />
          ) : null}

          <footer className="setup-footer">
            <div className="setup-footer-left">
              {settingsEntry && onExitSettings ? <button id="setup-back-btn" className="btn btn-compact" type="button" onClick={onExitSettings}>Back to dashboard</button> : null}
              <button className="btn btn-ghost btn-compact" type="button" onClick={() => void copyDiagnostics()}>{copyLabel}</button>
            </div>
            <div className="setup-actions">
              {busy && (mutationsAvailable || (packagedMode && downloadPhase === "downloading")) ? <button className="btn" type="button" onClick={() => void cancel()}>Cancel</button> : null}
              {progress.lifecycle === "failed" && !busy ? <button className="btn" type="button" onClick={() => void retryFailedAttempt()}>Retry</button> : null}
              {showRestart ? <button className="btn btn-primary setup-primary" type="button" disabled={busy || !mutationsAvailable} onClick={() => void restart()}>Restart Orpheus</button> : installedReady ? <button className="btn btn-primary setup-primary" type="button" disabled={adoptDisabled} onClick={() => void adoptInstalled()}>Continue</button> : packagedMode && selection.profile === "base" ? <button className="btn btn-primary setup-primary" type="button" disabled={busy || adopting} onClick={() => void continueLibraryOnly()}>Use library only</button> : packagedMode && backendInstallProfile !== null && selection.profile === backendInstallProfile ? <button className="btn btn-primary setup-primary" type="button" disabled={!backendReadyToStart} onClick={() => void installFromBackend()}>Install</button> : <button className="btn btn-primary setup-primary" type="button" disabled={!readyToStart} onClick={() => void start()}>{selection.profile === "base" ? "Use library only" : "Install"}</button>}
            </div>
          </footer>
        </section>
      </div>
    </main>
  );
}

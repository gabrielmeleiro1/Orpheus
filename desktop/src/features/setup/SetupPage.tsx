import { useCallback, useEffect, useMemo, useRef, useState } from "react";

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
  canStartSetup,
  createSelection,
  diagnosticsSnapshot,
  MODEL_GROUP_DETAILS,
  profileChoices,
  selectProfile,
  toggleModelGroup,
  type SetupProgressEvent,
  type SetupProgressState,
  type SetupSelection,
} from "./setupModel";
import { restartAndReconnectSetup } from "./setupLifecycle";

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

const EMPTY_PROGRESS: SetupProgressState = {
  lifecycle: "idle",
  stage: null,
  message: "Choose the local capabilities you want to install.",
  percent: null,
  completedStages: [],
  restartRequired: false,
};

const STAGES = [
  { id: "packages", label: "Runtime packages" },
  { id: "models", label: "Model downloads" },
  { id: "verify", label: "Compatibility check" },
];

function safeError(error: unknown, fallback: string): string {
  const value = error instanceof Error ? error.message : typeof error === "string" ? error : fallback;
  return value.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 512) || fallback;
}

const INSTALLED_PROFILE_LABEL: Record<"mlx" | "cuda", string> = { mlx: "MLX", cuda: "CUDA" };

function formatFreedBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 bytes";
  const units = ["bytes", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = unit === 0 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[unit]}`;
}

function StageState({ id, progress }: { id: string; progress: SetupProgressState }) {
  const complete = progress.completedStages.includes(id);
  const active = progress.lifecycle === "running" && progress.stage === id;
  return (
    <li className={`setup-stage${complete ? " is-complete" : active ? " is-active" : ""}`}>
      <span aria-hidden="true">{complete ? "✓" : active ? "◌" : "·"}</span>
      <span>{STAGES.find((stage) => stage.id === id)?.label ?? id}</span>
    </li>
  );
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
  const [deleteArmed, setDeleteArmed] = useState(false);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteResult, setDeleteResult] = useState<string | null>(null);
  const startLocked = useRef(false);
  const restartLocked = useRef(false);
  const deleteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const preRestartClient = useRef<ApiClient | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const setupHeadingRef = useRef<HTMLHeadingElement>(null);
  const progressHeadingRef = useRef<HTMLParagraphElement>(null);

  const refreshStatus = useCallback(async () => {
    if (!apiClient) {
      setError("Runtime setup status is temporarily unavailable.");
      return null;
    }
    try {
      const next = await apiClient.getRuntimeSetupStatus({ timeoutMs: 8_000 });
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
  const adoptDisabled = adopting || (installedIsCuda && !selection.acceptPytorchRisk);
  const readyToStart = Boolean(listenerReady && runnerReconciled && mutationsAvailable && status && canStartSetup(selection, status) && !busy && !startLocked.current);
  const showRestart = status?.restart_required || progress.restartRequired;
  const apiUnavailable = !status || Boolean(error);
  const progressValue = progress.percent === null ? undefined : progress.percent;

  const selectedEstimate = useMemo(() => selection.modelGroups
    .map((group) => MODEL_GROUP_DETAILS[group]?.estimate)
    .filter(Boolean)
    .join(" + "), [selection.modelGroups]);
  const progressMessage = showRestart && progress.lifecycle === "idle"
    ? "Installation complete. Restart Orpheus to load the selected runtime."
    : progress.message;

  const start = async () => {
    if (!mutationsAvailable || !status || !canStartSetup(selection, status) || !acquireSetupStart(startLocked)) return;
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
  };

  const cancel = async () => {
    if (!busy || !mutationsAvailable) return;
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
            <p className="setup-lede">Add a private, on-device runtime for speech and text cleaning — or start with the library only. You can change this later in Settings.</p>
          </div>

          {installedReady && installedRuntime ? (
            <div className="setup-installed" role="note">
              <strong>Installed {INSTALLED_PROFILE_LABEL[installedRuntime.profile]} runtime ready</strong>
              <p>This build includes a working local runtime — no download needed.</p>
            </div>
          ) : null}

          {apiUnavailable ? (
            <div className="setup-recovery" role="alert">
              <div><strong>Setup status unavailable</strong><p>{error ?? "The backend has not returned setup details yet."}</p></div>
              <div className="setup-actions"><button className="btn" type="button" onClick={() => void refreshStatus()}>Retry</button><button className="btn" type="button" onClick={() => void continueLibraryOnly()}>Continue in library-only mode</button></div>
            </div>
          ) : null}

          {environment && !environment.available && !installedReady ? (
            <div className="setup-recovery setup-environment" role="note">
              <div><strong>Install from a source checkout</strong><p>{environment.reason}</p><p>Clone Orpheus, create its <code>.venv</code>, then run <code>.venv/bin/python scripts/setup_runtime.py --profile base</code> (Windows: <code>.venv\Scripts\python.exe scripts\setup_runtime.py --profile base</code>).</p></div>
              <button className="btn" type="button" onClick={() => void continueLibraryOnly()}>Continue in library-only mode</button>
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

          {status ? (
            <>
              <fieldset className="setup-section" disabled={busy}>
                <legend>Runtime profile</legend>
                <div className="setup-profile-grid">
                  {choices.map((profile) => (
                    <label key={profile.id} className={`setup-profile-card${selection.profile === profile.id ? " is-selected" : ""}${profile.disabled ? " is-disabled" : ""}`}>
                      <input type="radio" name="runtime-profile" value={profile.id} checked={selection.profile === profile.id} disabled={profile.disabled} onChange={() => setSelection((current) => selectProfile(current, profile.id, status))} />
                      <span className="setup-card-radio" aria-hidden="true" />
                      <span className="setup-card-copy"><span className="setup-card-head"><strong>{profile.label}</strong>{status.recommended_profile === profile.id && profile.compatible ? <span className="setup-chip">Recommended</span> : null}</span><span>{profile.description}</span>{profile.reason ? <small>{profile.reason}</small> : null}</span>
                    </label>
                  ))}
                </div>
              </fieldset>

              <fieldset className="setup-section setup-model-section" disabled={busy || !selectedProfile || selectedProfile.model_groups.length === 0}>
                <legend>Local capabilities</legend>
                {selectedProfile?.model_groups.length ? (
                  <div className="setup-model-list">
                    {selectedProfile.model_groups.map((groupId) => {
                      const details = MODEL_GROUP_DETAILS[groupId];
                      if (!details) return null;
                      return (
                        <label className="setup-model-row" key={groupId}>
                          <input type="checkbox" checked={selection.modelGroups.includes(groupId)} onChange={() => setSelection((current) => toggleModelGroup(current, groupId, status))} />
                          <span><strong>{details.label}</strong><small>{details.description}</small></span>
                          <span className="setup-estimate"><em>Estimate</em>{details.estimate}</span>
                        </label>
                      );
                    })}
                  </div>
                ) : <p className="setup-empty-models">Library-only mode installs no local models — add a runtime later in Settings.</p>}
                {selectedEstimate ? <p className="setup-estimate-note"><strong>Estimates:</strong> {selectedEstimate}. Actual size varies by model revision.</p> : null}
              </fieldset>

              {showRiskBlock && riskProfile?.risk ? (
                <div className="setup-risk" role="note">
                  <strong>PyTorch security notice</strong><p>{riskProfile.risk.summary}</p>
                  <label><input type="checkbox" checked={selection.acceptPytorchRisk} disabled={busy || adopting} onChange={(event) => setSelection((current) => ({ ...current, acceptPytorchRisk: event.target.checked }))} /> I understand and will use only trusted model files.</label>
                </div>
              ) : null}
            </>
          ) : null}

          <p className="setup-privacy">Private by default — models run on this device; the network is used only for downloads.</p>

          {(busy || progress.lifecycle !== "idle" || showRestart) ? (
            <section className="setup-progress" aria-labelledby="setup-progress-heading">
              <div><p className="setup-section-label" id="setup-progress-heading" ref={progressHeadingRef} tabIndex={-1}>Installation</p><p className="setup-progress-message" aria-live="polite">{progressMessage}</p></div>
              <progress value={progressValue} max={100} aria-label={progressValue === undefined ? "Setup in progress" : `Setup ${Math.round(progressValue)} percent complete`} />
              <ol>{STAGES.map(({ id }) => <StageState key={id} id={id} progress={progress} />)}</ol>
            </section>
          ) : null}

          {error && status ? <p className="setup-error" role="alert" ref={errorRef} tabIndex={-1}>{error}</p> : null}

          {settingsEntry && !busy ? (
            <section className="setup-danger" aria-labelledby="setup-danger-heading">
              <p className="setup-section-label" id="setup-danger-heading">Local data</p>
              <div className="setup-danger-box">
                <p>Delete downloaded models and reset runtime setup.</p>
                <div className="setup-danger-actions">
                  <button
                    className="btn setup-danger-btn"
                    type="button"
                    disabled={deletePending}
                    onClick={onDeleteClick}
                    onBlur={disarmDelete}
                    onKeyDown={(event) => { if (event.key === "Escape") disarmDelete(); }}
                  >
                    {deletePending ? "Deleting…" : deleteArmed ? "Confirm delete" : "Delete local runtime data"}
                  </button>
                  {deleteResult ? <span className="setup-danger-note">{deleteResult}</span> : null}
                </div>
              </div>
            </section>
          ) : null}

          <footer className="setup-footer">
            <div className="setup-footer-left">
              {settingsEntry && onExitSettings ? <button id="setup-back-btn" className="btn btn-compact" type="button" onClick={onExitSettings}>Back to dashboard</button> : null}
              <button className="btn btn-compact setup-tertiary" type="button" onClick={() => void copyDiagnostics()}>{copyLabel}</button>
            </div>
            <div className="setup-actions">
              {busy ? <button className="btn" type="button" disabled={!mutationsAvailable} onClick={() => void cancel()}>Cancel</button> : null}
              {progress.lifecycle === "failed" && !busy ? <button className="btn" type="button" onClick={() => { setError(null); setProgress(EMPTY_PROGRESS); }}>Retry</button> : null}
              {showRestart ? <button className="btn btn-primary setup-primary" type="button" disabled={busy || !mutationsAvailable} onClick={() => void restart()}>Restart Orpheus</button> : installedReady ? <button className="btn btn-primary setup-primary" type="button" disabled={adoptDisabled} onClick={() => void adoptInstalled()}>Continue with installed runtime</button> : <button className="btn btn-primary setup-primary" type="button" disabled={!readyToStart} onClick={() => void start()}>{selection.profile === "base" ? "Use library-only mode" : "Install selected runtime"}</button>}
            </div>
          </footer>
        </section>
      </div>
    </main>
  );
}

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
import { cancelActiveSetup, restartAndReconnectSetup } from "./setupLifecycle";

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

function progressFromRunner(status: RuntimeSetupRunnerStatus, previous: SetupProgressState): SetupProgressState {
  if (status.lifecycle === "running" || status.lifecycle === "cancelling") {
    return { ...previous, lifecycle: "running", message: status.lifecycle === "cancelling" ? "Cancelling setup…" : previous.message };
  }
  if (status.lifecycle === "complete") {
    return { ...previous, lifecycle: "complete", message: "Setup complete.", restartRequired: status.restartRequired };
  }
  if (["failed", "cancelled", "timed_out"].includes(status.lifecycle)) {
    return { ...previous, lifecycle: "failed", message: safeError(status.error, status.lifecycle === "cancelled" ? "Setup cancelled." : "Setup failed.") };
  }
  return previous;
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
  const [awaitingReconnect, setAwaitingReconnect] = useState(false);
  const startLocked = useRef(false);
  const restartLocked = useRef(false);
  const preRestartClient = useRef<ApiClient | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
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
      setProgress((previous) => progressFromRunner(runner, previous));
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
    if (!environment?.available) return undefined;
    let mounted = true;
    let release: (() => void) | null = null;
    void bridge.listen((event) => {
      if (!mounted) return;
      setProgress((previous) => applyProgressEvent(previous, event));
      if (event.terminal) {
        void refreshRunner();
      }
    }).then((unlisten) => {
      if (mounted) release = unlisten;
      else unlisten();
    }).catch(() => undefined);
    void refreshRunner();
    return () => {
      mounted = false;
      release?.();
    };
  }, [bridge, environment?.available, refreshRunner]);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  useEffect(() => {
    if (busy || progress.lifecycle === "complete" || progress.lifecycle === "failed") {
      progressHeadingRef.current?.focus();
    }
  }, [busy, progress.lifecycle]);

  useEffect(() => {
    if (!busy) return undefined;
    const timer = window.setInterval(() => void refreshRunner(), 1_000);
    return () => window.clearInterval(timer);
  }, [busy, refreshRunner]);

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
  const readyToStart = Boolean(mutationsAvailable && status && canStartSetup(selection, status) && !busy && !startLocked.current);
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
      setProgress((previous) => progressFromRunner(runner, previous));
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
      const runner = await cancelActiveSetup(bridge.cancel);
      setProgress((previous) => progressFromRunner(runner, previous));
      setBusy(false);
      startLocked.current = false;
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

  return (
    <main className="app-frame setup-frame" aria-label="Orpheus runtime setup">
      <aside className="rail setup-rail" aria-label="Orpheus setup navigation">
        <div className="brand"><span className="brand-mark" aria-hidden="true">O</span><span className="brand-name">Orpheus</span></div>
        <nav className="rail-nav" aria-label="Setup sections">
          <button className="rail-tab active" type="button" aria-current="page">
            <span className="rail-icon" aria-hidden="true">✦</span><span className="rail-label">Runtime setup</span>
          </button>
        </nav>
        <div className="rail-bottom"><span className="setup-rail-note">Local models are optional. Library tools remain available.</span></div>
      </aside>

      <div className="workspace-shell">
        <header className="studio-status setup-status-header">
          <div><h1>orpheus.local</h1><p>First-run setup · choose only what you need</p></div>
          <div className="studio-controls"><span className={`status-dot${apiClient ? " is-connected" : ""}`} aria-hidden="true" /><span className="control">setup</span></div>
        </header>

        <div className="setup-workspace">
          <section className="setup-panel" aria-labelledby="setup-heading">
            <div className="panel-header"><span>configuration</span><span className="panel-header-meta">step 1 of 1</span></div>
            <div className="setup-intro">
              <div><p className="setup-eyebrow">WELCOME TO ORPHEUS</p><h2 id="setup-heading">Choose your runtime</h2><p>Document conversion and playback work without local AI. Add a compatible runtime for private, on-device speech and text cleaning.</p></div>
              <span className="setup-private-chip">downloads from trusted model repositories</span>
            </div>

            {apiUnavailable ? (
              <div className="setup-recovery" role="alert">
                <div><strong>Setup status unavailable</strong><p>{error ?? "The backend has not returned setup details yet."}</p></div>
                <div className="setup-actions"><button className="btn" type="button" onClick={() => void refreshStatus()}>Retry</button><button className="btn" type="button" onClick={onContinueLibraryOnly}>Continue in library-only mode</button></div>
              </div>
            ) : null}

            {environment && !environment.available ? (
              <div className="setup-recovery setup-environment" role="note">
                <div><strong>Install from a source checkout</strong><p>{environment.reason}</p><p>For dynamic profiles, clone Orpheus, create its fixed <code>.venv</code>, then run <code>.venv/bin/python scripts/setup_runtime.py --profile base</code> (Windows: <code>.venv\Scripts\python.exe scripts\setup_runtime.py --profile base</code>). Packaged builds use the runtime profile included at build time.</p></div>
                <button className="btn" type="button" onClick={onContinueLibraryOnly}>Continue in library-only mode</button>
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
                        <span className="setup-card-copy"><strong>{profile.label}</strong><span>{profile.description}</span>{profile.reason ? <small>{profile.reason}</small> : null}</span>
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
                  ) : <p className="setup-empty-models">Library-only mode does not install local models. You can add a runtime later in Settings.</p>}
                  {selectedEstimate ? <p className="setup-estimate-note"><strong>Estimates:</strong> {selectedEstimate}. Actual transfer and disk use vary by model revision and cache state.</p> : null}
                </fieldset>

                {selection.profile === "cuda" && selectedProfile?.risk ? (
                  <div className="setup-risk" role="note">
                    <strong>PyTorch security notice</strong><p>{selectedProfile.risk.summary}</p>
                    <label><input type="checkbox" checked={selection.acceptPytorchRisk} disabled={busy} onChange={(event) => setSelection((current) => ({ ...current, acceptPytorchRisk: event.target.checked }))} /> I understand and will use only trusted model files.</label>
                  </div>
                ) : null}
              </>
            ) : null}

            <div className="setup-privacy"><span aria-hidden="true">⌁</span><p><strong>Private by default.</strong> Local model content stays on this device. Network access is used only to install packages and download the model groups selected above.</p></div>

            {(busy || progress.lifecycle !== "idle" || showRestart) ? (
              <section className="setup-progress" aria-labelledby="setup-progress-heading">
                <div><p className="setup-eyebrow" id="setup-progress-heading" ref={progressHeadingRef} tabIndex={-1}>INSTALLATION</p><p className="setup-progress-message" aria-live="polite">{progressMessage}</p></div>
                <progress value={progressValue} max={100} aria-label={progressValue === undefined ? "Setup in progress" : `Setup ${Math.round(progressValue)} percent complete`} />
                <ol>{STAGES.map(({ id }) => <StageState key={id} id={id} progress={progress} />)}</ol>
              </section>
            ) : null}

            {error && status ? <p className="setup-error" role="alert" ref={errorRef} tabIndex={-1}>{error}</p> : null}

            <footer className="setup-footer">
              <button className="btn btn-compact" type="button" onClick={() => void copyDiagnostics()}>{copyLabel}</button>
              <div className="setup-actions">
                {busy ? <button className="btn" type="button" disabled={!mutationsAvailable} onClick={() => void cancel()}>Cancel</button> : null}
                {progress.lifecycle === "failed" && !busy ? <button className="btn" type="button" onClick={() => { setError(null); setProgress(EMPTY_PROGRESS); }}>Retry</button> : null}
                {showRestart ? <button className="btn btn-primary setup-primary" type="button" disabled={busy || !mutationsAvailable} onClick={() => void restart()}>Restart Orpheus</button> : <button className="btn btn-primary setup-primary" type="button" disabled={!readyToStart} onClick={() => void start()}>{selection.profile === "base" ? "Use library-only mode" : "Install selected runtime"}</button>}
              </div>
            </footer>
          </section>
        </div>
      </div>
    </main>
  );
}

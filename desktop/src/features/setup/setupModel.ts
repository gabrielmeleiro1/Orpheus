import type {
  RuntimeModelDownloadStatus,
  RuntimeSetupInstalledRuntime,
  RuntimeSetupProfile,
  RuntimeSetupStatusResponse,
} from "../../types/api";

export type RuntimeProfileId = RuntimeSetupProfile["id"];

export interface SetupSelection {
  cleanerCudaQuantization?: string;
  profile: RuntimeProfileId;
  modelGroups: string[];
  acceptPytorchRisk: boolean;
}

export interface SetupProfileChoice extends RuntimeSetupProfile {
  disabled: boolean;
}

export interface SetupProgressEvent {
  event: string;
  stage?: string;
  message?: string;
  percent?: number;
  terminal?: boolean;
  restart_required?: boolean;
}

export interface SetupRunnerStatusSnapshot {
  lifecycle: "idle" | "running" | "cancelling" | "complete" | "failed" | "cancelled" | "timed_out";
  profile: string | null;
  modelGroups: string[];
  stage: string | null;
  message: string | null;
  percent: number | null;
  restartRequired: boolean;
  error: string | null;
}

export interface SetupProgressState {
  lifecycle: "idle" | "running" | "complete" | "failed";
  stage: string | null;
  message: string;
  percent: number | null;
  completedStages: string[];
  restartRequired: boolean;
}

export interface SetupStartLock {
  current: boolean;
}

export function acquireSetupStart(lock: SetupStartLock): boolean {
  if (lock.current) return false;
  lock.current = true;
  return true;
}

export interface ModelGroupDetail {
  label: string;
  description: string;
  /** HF repo actually fetched — mirrors MODEL_GROUPS in scripts/download_models.py. */
  model: string;
  downloadMB: number;
  diskMB: number;
  kind: "tts" | "cleaner";
}

export const MODEL_GROUP_DETAILS: Record<string, ModelGroupDetail> = {
  "tts-mlx": { label: "Kokoro TTS", description: "Natural speech generated locally on Apple Silicon.", model: "mlx-community/Kokoro-82M-bf16", downloadMB: 450, diskMB: 700, kind: "tts" },
  "cleaner-mlx": { label: "Llama 3.2 · 3B", description: "Larger text cleaner, supplied in 4-bit precision.", model: "mlx-community/Llama-3.2-3B-Instruct-4bit", downloadMB: 1800, diskMB: 2300, kind: "cleaner" },
  "tts-cpu-cuda": { label: "Kokoro TTS", description: "Natural speech generated with the CUDA runtime.", model: "hexgrad/Kokoro-82M", downloadMB: 500, diskMB: 900, kind: "tts" },
  "cleaner-cuda": { label: "Qwen 2.5 · 1.5B", description: "Recommended balance of size and text-cleaning capability.", model: "Qwen/Qwen2.5-1.5B-Instruct", downloadMB: 3100, diskMB: 3200, kind: "cleaner" },
  "cleaner-cuda-large": { label: "Qwen 2.5 · 3B", description: "Larger model for more demanding text; uses more GPU memory.", model: "Qwen/Qwen2.5-3B-Instruct", downloadMB: 6200, diskMB: 6400, kind: "cleaner" },
  "cleaner-mlx-small": { label: "Llama 3.2 · 1B", description: "Smaller text cleaner, supplied in 4-bit precision.", model: "mlx-community/Llama-3.2-1B-Instruct-4bit", downloadMB: 713, diskMB: 800, kind: "cleaner" },
};

function formatMB(mb: number): string {
  if (!Number.isFinite(mb) || mb <= 0) return "0 MB";
  return mb >= 1000 ? `${(mb / 1000).toFixed(1).replace(/\.0$/, "")} GB` : `${Math.round(mb)} MB`;
}

export function groupEstimate(groupId: string): string {
  const details = MODEL_GROUP_DETAILS[groupId];
  if (!details) return "";
  return `~${formatMB(details.downloadMB)} download · ~${formatMB(details.diskMB)} disk`;
}

export function selectionEstimate(modelGroups: string[]): string {
  const details = modelGroups
    .map((group) => MODEL_GROUP_DETAILS[group])
    .filter((entry): entry is ModelGroupDetail => Boolean(entry));
  if (details.length === 0) return "";
  const download = details.reduce((sum, entry) => sum + entry.downloadMB, 0);
  const disk = details.reduce((sum, entry) => sum + entry.diskMB, 0);
  return `~${formatMB(download)} download · ~${formatMB(disk)} on disk`;
}

// Download one voice model and one cleaner, never the whole catalog.
function defaultModelGroups(profile: RuntimeSetupProfile): string[] {
  return ["tts", "cleaner"].flatMap((kind) => {
    const group = profile.model_groups.find((id) => MODEL_GROUP_DETAILS[id]?.kind === kind);
    return group ? [group] : [];
  });
}

export function profileChoices(status: RuntimeSetupStatusResponse): SetupProfileChoice[] {
  return status.profiles.map((profile) => ({ ...profile, disabled: !profile.compatible }));
}

export function createSelection(status: RuntimeSetupStatusResponse): SetupSelection {
  const suggested = status.recommended_profile;
  const recommended = status.selected_profile == null && suggested
    ? status.profiles.find((profile) => profile.id === suggested && profile.compatible)
    : undefined;
  const selected = status.profiles.find((profile) => profile.id === status.selected_profile && profile.compatible)
    ?? recommended
    ?? status.profiles.find((profile) => profile.id === "base" && profile.compatible)
    ?? status.profiles.find((profile) => profile.compatible)
    ?? status.profiles[0];
  const allowed = new Set(selected?.model_groups ?? []);
  const persisted = status.selected_model_groups.map(({ id }) => id).filter((id) => allowed.has(id));
  const groups = status.configured && selected?.id === status.selected_profile ? persisted
    : selected ? defaultModelGroups(selected) : [];
  const seen = new Set<string>();
  return {
    profile: selected?.id ?? "base",
    modelGroups: groups.filter((id) => {
      const kind = MODEL_GROUP_DETAILS[id]?.kind ?? id;
      if (seen.has(kind)) return false;
      seen.add(kind);
      return true;
    }),
    acceptPytorchRisk: Boolean(selected?.risk?.accepted),
    cleanerCudaQuantization: status.cleaner_cuda_quantization ?? "4bit",
  };
}

export function selectProfile(
  selection: SetupSelection,
  profileId: RuntimeProfileId,
  status: RuntimeSetupStatusResponse,
): SetupSelection {
  const profile = status.profiles.find((candidate) => candidate.id === profileId);
  if (!profile?.compatible) {
    return selection;
  }
  return {
    profile: profile.id,
    modelGroups: defaultModelGroups(profile),
    acceptPytorchRisk: Boolean(profile.risk?.accepted),
    cleanerCudaQuantization: selection.cleanerCudaQuantization ?? "4bit",
  };
}

export function toggleModelGroup(
  selection: SetupSelection,
  groupId: string,
  status: RuntimeSetupStatusResponse,
): SetupSelection {
  const profile = status.profiles.find((candidate) => candidate.id === selection.profile);
  if (!profile?.compatible || !profile.model_groups.includes(groupId)) {
    return selection;
  }
  return {
    ...selection,
    modelGroups: selection.modelGroups.includes(groupId)
      ? selection.modelGroups.filter((item) => item !== groupId)
      : [...selection.modelGroups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind !== MODEL_GROUP_DETAILS[groupId]?.kind), groupId],
  };
}

export function canStartSetup(selection: SetupSelection, status: RuntimeSetupStatusResponse): boolean {
  const profile = status.profiles.find((candidate) => candidate.id === selection.profile);
  if (!profile?.compatible || selection.modelGroups.some((group) => !profile.model_groups.includes(group))) {
    return false;
  }
  if (profile.id !== "base" && !selection.modelGroups.some((id) => MODEL_GROUP_DETAILS[id]?.kind === "tts")) {
    return false;
  }
  return !profile.risk || selection.acceptPytorchRisk;
}

export function backendInstallProfileId(
  environmentAvailable: boolean,
  installedRuntime: RuntimeSetupInstalledRuntime | null | undefined,
): string | null {
  // The desktop installer owns provisioning when it is available; the
  // backend-download path exists only for packaged builds whose baked runtime
  // works but whose model assets are not downloaded yet.
  if (environmentAvailable) {
    return null;
  }
  if (!installedRuntime || (!installedRuntime.runtime_available && !installedRuntime.model_groups.some((group) => group.runtime_ready))) {
    return null;
  }
  return installedRuntime.profile;
}

export function selectionMatchesConfigured(selection: SetupSelection, status: RuntimeSetupStatusResponse): boolean {
  const configured = new Set(status.selected_model_groups.map(({ id }) => id));
  return status.configured && selection.profile === status.selected_profile
    && (selection.cleanerCudaQuantization ?? "4bit") === (status.cleaner_cuda_quantization ?? "4bit")
    && configured.size === selection.modelGroups.length
    && selection.modelGroups.every((id) => configured.has(id));
}

/** Readiness belongs to each requested model, not just the profile's TTS. */
export function selectionReadiness(selection: SetupSelection, status: RuntimeSetupStatusResponse): { runtimeReady: boolean; ready: boolean } {
  const groups = status.installed_runtime?.profile === selection.profile
    ? status.installed_runtime.model_groups
    : status.selected_profile === selection.profile ? status.selected_model_groups : [];
  const selected = selection.modelGroups.map((id) => groups.find((group) => group.id === id));
  const valid = canStartSetup(selection, status);
  return {
    runtimeReady: valid && selected.every((group) => group?.runtime_ready),
    ready: valid && selected.every((group) => group?.ready),
  };
}

export function modelDownloadProgressEvent(
  status: RuntimeModelDownloadStatus,
): SetupProgressEvent {
  if (status.lifecycle === "stopping") {
    return {
      event: "status",
      stage: "models",
      message: "Stopping download… You can leave setup while it stops.",
    };
  }
  if (status.lifecycle === "cancelled") {
    return {
      event: "status",
      stage: "models",
      message: "Model download cancelled.",
    };
  }
  if (status.lifecycle === "failed") {
    return {
      event: "error",
      terminal: true,
      message: status.error ?? "Model download failed.",
    };
  }
  if (status.lifecycle === "complete") {
    return {
      event: "stage_complete",
      stage: "models",
      message: "Model downloads complete. Verifying the runtime…",
      percent: 99,
    };
  }
  // A malformed snapshot without counts must not surface "(NaN of NaN)".
  const hasCounts = Number.isFinite(status.total_models) && Number.isFinite(status.completed_models);
  const totalModels = hasCounts ? Math.max(status.total_models, 1) : 1;
  const position = hasCounts ? Math.min(status.completed_models + 1, totalModels) : 1;
  const label = MODEL_GROUP_DETAILS[status.current_model_group ?? ""]?.label
    ?? status.current_model_group
    ?? "models";
  return {
    event: "status",
    stage: "models",
    message: hasCounts ? `Downloading ${label} (${position} of ${totalModels})…` : `Downloading ${label}…`,
    percent: hasCounts && status.total_models > 0
      ? Math.round((status.completed_models / status.total_models) * 100)
      : undefined,
  };
}

export function shouldShowRuntimeSetup(
  status: RuntimeSetupStatusResponse | null,
  libraryOnlyOverride: boolean,
): boolean {
  if (libraryOnlyOverride) {
    return false;
  }
  return !status || !status.configured || !status.ready || status.restart_required || status.state !== "ready";
}

function boundedMessage(value: unknown): string {
  const message = typeof value === "string" ? value.replace(/[\u0000-\u001f\u007f]/g, " ").trim() : "";
  return message.slice(0, 512);
}

function normalizedStage(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const safe = value.slice(0, 64);
  const normalized = safe.toLowerCase();
  if (normalized === "dependency installation" || normalized === "dependency_installation") return "packages";
  if (normalized === "model download" || normalized === "model_download") return "models";
  if (normalized.includes("verify") || normalized.includes("verification") || normalized.includes("validation")) return "verify";
  return safe;
}

const SETUP_STAGE_ORDER = ["packages", "models", "verify"] as const;

function inferCompletedStages(completedStages: string[], currentStage: string | null): string[] {
  const stageIndex = SETUP_STAGE_ORDER.indexOf(currentStage as typeof SETUP_STAGE_ORDER[number]);
  if (stageIndex <= 0) return completedStages;

  const next = [...completedStages];
  for (const stage of SETUP_STAGE_ORDER.slice(0, stageIndex)) {
    if (!next.includes(stage)) next.push(stage);
  }
  return next;
}

export function applyProgressEvent(
  previous: SetupProgressState | undefined,
  event: SetupProgressEvent,
): SetupProgressState {
  const prior: SetupProgressState = previous ?? {
    lifecycle: "idle",
    stage: null,
    message: "Ready to configure Orpheus.",
    percent: null,
    completedStages: [],
    restartRequired: false,
  };
  const stage = normalizedStage(event.stage) ?? prior.stage;
  let completedStages = inferCompletedStages(prior.completedStages, stage);
  if (event.event === "stage_complete" && stage && !completedStages.includes(stage)) {
    completedStages = [...completedStages, stage];
  }
  const terminal = Boolean(event.terminal);
  const terminalSuccess = terminal && event.event === "complete";
  if (terminal && event.event === "complete") {
    completedStages = ["packages", "models", "verify"];
  }
  return {
    lifecycle: terminal ? (event.event === "complete" ? "complete" : "failed") : "running",
    stage,
    message: boundedMessage(event.message) || (terminal && event.event === "complete" ? "Setup complete." : prior.message),
    percent: terminalSuccess
      ? 100
      : typeof event.percent === "number" && Number.isFinite(event.percent)
        ? Math.max(0, Math.min(99, event.percent))
        : prior.percent,
    completedStages,
    restartRequired: Boolean(event.restart_required) || prior.restartRequired,
  };
}

export function applyRunnerStatus(
  previous: SetupProgressState | undefined,
  status: SetupRunnerStatusSnapshot,
): SetupProgressState {
  const snapshot: SetupProgressEvent = {
    event: "status",
    stage: status.stage ?? undefined,
    message: status.message ?? undefined,
    percent: status.percent ?? undefined,
    restart_required: status.restartRequired,
  };
  const prior = { ...(previous ?? {
    lifecycle: "idle" as const,
    stage: null,
    message: "Ready to configure Orpheus.",
    percent: null,
    completedStages: [],
    restartRequired: false,
  }), restartRequired: status.restartRequired };

  if (status.lifecycle === "running" || status.lifecycle === "cancelling") {
    const next = applyProgressEvent(prior, snapshot);
    return status.lifecycle === "cancelling"
      ? { ...next, message: status.stage === "models" || status.stage === "Model download" ? "Stopping download… You can leave setup while it stops." : "Cancelling setup… You can leave setup while it stops." }
      : next;
  }
  if (status.lifecycle === "complete") {
    return applyProgressEvent(prior, {
      ...snapshot,
      event: "complete",
      terminal: true,
      percent: 100,
      message: status.message ?? "Setup complete.",
    });
  }
  if (["failed", "cancelled", "timed_out"].includes(status.lifecycle)) {
    return applyProgressEvent(prior, {
      ...snapshot,
      event: "error",
      terminal: true,
      message: status.error ?? status.message ?? (status.lifecycle === "cancelled" ? "Setup cancelled." : "Setup failed."),
    });
  }
  return prior;
}

export function diagnosticsSnapshot(
  status: RuntimeSetupStatusResponse | null,
  progress: SetupProgressState,
): string {
  return JSON.stringify({
    schema_version: 1,
    setup_state: status?.state ?? "endpoint_unavailable",
    configured: status?.configured ?? false,
    selected_profile: status?.selected_profile ?? null,
    selected_model_groups: status?.selected_model_groups.map(({ id, ready, assets_reason }) => ({ id, ready, assets_reason })) ?? [],
    package_issue_names: status ? [...status.packages.missing, ...status.packages.incompatible].map(({ name }) => name) : [],
    backend_instance: status?.backend.instance_id ?? null,
    progress,
  }, null, 2);
}

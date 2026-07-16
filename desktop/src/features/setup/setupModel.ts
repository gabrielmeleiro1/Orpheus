import type {
  RuntimeSetupProfile,
  RuntimeSetupStatusResponse,
} from "../../types/api";

export type RuntimeProfileId = RuntimeSetupProfile["id"];

export interface SetupSelection {
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

export const MODEL_GROUP_DETAILS: Record<string, { label: string; description: string; estimate: string; kind: "tts" | "cleaner" }> = {
  "tts-mlx": { label: "Kokoro TTS · MLX", description: "Natural speech generated locally on Apple Silicon.", estimate: "~450 MB download · ~700 MB disk", kind: "tts" },
  "cleaner-mlx": { label: "Qwen Cleaner · MLX", description: "Optional local text cleanup before synthesis.", estimate: "~1.8 GB download · ~2.3 GB disk", kind: "cleaner" },
  "tts-cpu-cuda": { label: "Kokoro TTS · PyTorch", description: "Natural speech generated with the CUDA runtime.", estimate: "~500 MB download · ~900 MB disk", kind: "tts" },
  "cleaner-cuda": { label: "Qwen Cleaner · CUDA", description: "Optional GPU-accelerated local text cleanup.", estimate: "~2.2 GB download · ~3.0 GB disk", kind: "cleaner" },
};

function defaultTtsGroup(profile: RuntimeSetupProfile): string[] {
  const tts = profile.model_groups.find((group) => MODEL_GROUP_DETAILS[group]?.kind === "tts");
  return tts ? [tts] : [];
}

export function profileChoices(status: RuntimeSetupStatusResponse): SetupProfileChoice[] {
  return status.profiles.map((profile) => ({ ...profile, disabled: !profile.compatible }));
}

export function createSelection(status: RuntimeSetupStatusResponse): SetupSelection {
  const selected = status.profiles.find((profile) => profile.id === status.selected_profile && profile.compatible)
    ?? status.profiles.find((profile) => profile.id === "base" && profile.compatible)
    ?? status.profiles.find((profile) => profile.compatible)
    ?? status.profiles[0];
  const allowed = new Set(selected?.model_groups ?? []);
  const persisted = status.selected_model_groups.map(({ id }) => id).filter((id) => allowed.has(id));
  return {
    profile: selected?.id ?? "base",
    modelGroups: persisted.length > 0 ? persisted : selected ? defaultTtsGroup(selected) : [],
    acceptPytorchRisk: Boolean(selected?.risk?.accepted),
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
    modelGroups: defaultTtsGroup(profile),
    acceptPytorchRisk: Boolean(profile.risk?.accepted),
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
      : [...selection.modelGroups, groupId],
  };
}

export function canStartSetup(selection: SetupSelection, status: RuntimeSetupStatusResponse): boolean {
  const profile = status.profiles.find((candidate) => candidate.id === selection.profile);
  if (!profile?.compatible || selection.modelGroups.some((group) => !profile.model_groups.includes(group))) {
    return false;
  }
  return profile.id !== "cuda" || selection.acceptPytorchRisk;
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
  let completedStages = event.event === "stage_complete" && stage && !prior.completedStages.includes(stage)
    ? [...prior.completedStages, stage]
    : prior.completedStages;
  const terminal = Boolean(event.terminal);
  if (terminal && event.event === "complete") {
    completedStages = ["packages", "models", "verify"];
  }
  return {
    lifecycle: terminal ? (event.event === "complete" ? "complete" : "failed") : "running",
    stage,
    message: boundedMessage(event.message) || (terminal && event.event === "complete" ? "Setup complete." : prior.message),
    percent: typeof event.percent === "number" && Number.isFinite(event.percent)
      ? Math.max(0, Math.min(100, event.percent))
      : prior.percent,
    completedStages,
    restartRequired: Boolean(event.restart_required) || prior.restartRequired,
  };
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

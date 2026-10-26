import { formatMB, MODEL_GROUP_DETAILS, type SetupProgressState } from "./setupModel";

export const EMPTY_PROGRESS: SetupProgressState = {
  lifecycle: "idle",
  stage: null,
  message: "Choose the local capabilities you want to install.",
  percent: null,
  completedStages: [],
  restartRequired: false,
};

export function safeError(error: unknown, fallback: string): string {
  const value = error instanceof Error ? error.message : typeof error === "string" ? error : fallback;
  return value.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 512) || fallback;
}

export function modelDownloadSize(groupId: string): string {
  return formatMB(MODEL_GROUP_DETAILS[groupId].downloadMB);
}

/** One-line reason a runtime cannot run here; the full backend reason stays available on hover. */
export function shortIncompatibleReason(profileId: string, reason: string | null | undefined): string {
  if (profileId === "mlx") return !reason || /arm64/.test(reason) ? "Mac only" : "Needs macOS 14";
  if (profileId === "cuda") return "Windows or Linux only";
  return "Not available here";
}

export function formatFreedBytes(bytes: number): string {
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

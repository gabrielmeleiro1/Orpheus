import type { SetupProgressState } from "./setupModel";

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

export const INSTALLED_PROFILE_LABEL: Record<"mlx" | "cuda", string> = { mlx: "MLX", cuda: "CUDA" };

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

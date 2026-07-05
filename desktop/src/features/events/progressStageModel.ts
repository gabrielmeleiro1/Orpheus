import type {
  DesktopEventPayload,
  ProgressStageDetail,
  ProgressStageName,
  ProgressStageView,
} from "../../types/api";

export const PROGRESS_STAGE_NAMES: ProgressStageName[] = [
  "ingestion",
  "chunking",
  "cleaning",
  "synthesis",
  "encoding",
  "packaging",
];

const BACKEND_STAGE_NAME_MAP: Record<string, ProgressStageName | ""> = {
  idle: "",
  ingesting: "ingestion",
  chunking: "chunking",
  cleaning: "cleaning",
  synthesizing: "synthesis",
  encoding: "encoding",
  packaging: "packaging",
  complete: "packaging",
  completed: "packaging",
  error: "",
  cancelled: "",
};

export function createProgressStages(): ProgressStageView[] {
  return PROGRESS_STAGE_NAMES.map((name) => ({
    name,
    state: "pending",
    pct: 0,
    detail: [],
  }));
}

export function normalizeProgressPercent(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 0;
  }

  if (numeric <= 1) {
    return Math.max(0, Math.min(100, Math.round(numeric * 100)));
  }

  return Math.max(0, Math.min(100, Math.round(numeric)));
}

function normalizeCounter(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return 0;
  }
  return Math.max(0, Math.round(numeric));
}

function counterPercent(current: number, total: number, fallbackPct = 0): number {
  if (total <= 0) {
    return normalizeProgressPercent(fallbackPct);
  }
  return normalizeProgressPercent(Math.min(total, current) / total);
}

export function formatEtaLabel(value: unknown): string {
  const totalSeconds = Math.round(Number(value));
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) {
    return "";
  }

  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const parts: string[] = [];

  if (hours > 0) {
    parts.push(`${hours}h`);
  }
  if (minutes > 0) {
    parts.push(`${minutes}m`);
  }
  if (parts.length === 0 || (hours === 0 && minutes < 5)) {
    parts.push(`${seconds}s`);
  }

  return `eta ~${parts.join(" ")}`;
}

export function normalizeStageName(stage: string): ProgressStageName | "" {
  const stageValue = stage.toLowerCase();
  if (stageValue in BACKEND_STAGE_NAME_MAP) {
    return BACKEND_STAGE_NAME_MAP[stageValue];
  }
  if (stageValue.includes("ingest")) {
    return "ingestion";
  }
  if (stageValue.includes("chunk")) {
    return "chunking";
  }
  if (stageValue.includes("clean")) {
    return "cleaning";
  }
  if (stageValue.includes("synth")) {
    return "synthesis";
  }
  if (stageValue.includes("encod")) {
    return "encoding";
  }
  if (stageValue.includes("pack")) {
    return "packaging";
  }
  if (stageValue === "complete" || stageValue === "completed") {
    return "packaging";
  }
  return "";
}

export function resolveStageIndex(stage: string): number {
  const normalizedStage = normalizeStageName(stage);
  const index = PROGRESS_STAGE_NAMES.indexOf(normalizedStage as ProgressStageName);
  return index >= 0 ? index : 0;
}

export function buildStageDetail(
  stage: string,
  payload: DesktopEventPayload,
  stagePct: number,
  message: string,
): ProgressStageDetail[] {
  const details: ProgressStageDetail[] = [];
  const normalizedStage = normalizeStageName(stage);
  const chapterCurrent = normalizeCounter(payload.chapter_current);
  const chapterTotal = normalizeCounter(payload.chapter_total);
  const chunkCurrent = normalizeCounter(payload.chunk_current);
  const chunkTotal = normalizeCounter(payload.chunk_total);

  if (chapterTotal > 0) {
    details.push({
      label: "chapter",
      value: `${Math.min(chapterCurrent, chapterTotal)}/${chapterTotal}`,
      pct: counterPercent(chapterCurrent, chapterTotal),
    });
  }

  if (chunkTotal > 0) {
    details.push({
      label: "chunk",
      value: `${Math.min(chunkCurrent, chunkTotal)}/${chunkTotal}`,
      pct: counterPercent(chunkCurrent, chunkTotal),
    });
  }

  if (details.length === 0) {
    const fallbackValue =
      normalizedStage === "packaging"
        ? "building final file"
        : normalizedStage === "encoding"
          ? "encoding audio"
          : message || "working";
    details.push({
      label: "task",
      value: fallbackValue,
      pct: stagePct,
    });
  }

  return details;
}

export function doneStages(): ProgressStageView[] {
  return createProgressStages().map((item) => ({ ...item, state: "done", pct: 100 }));
}

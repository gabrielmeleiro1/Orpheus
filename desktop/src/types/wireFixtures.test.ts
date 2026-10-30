import { describe, expect, it } from "vitest";
import activeCancelled from "../test/wire-fixtures/active-cancelled.json";
import activeCompleted from "../test/wire-fixtures/active-completed.json";
import activeIdle from "../test/wire-fixtures/active-idle.json";
import activeRunning from "../test/wire-fixtures/active-running.json";
import bookDetailEmpty from "../test/wire-fixtures/book-detail-empty.json";
import bookDetail from "../test/wire-fixtures/book-detail.json";
import conversionStart from "../test/wire-fixtures/conversion-start.json";
import health from "../test/wire-fixtures/health.json";
import libraryList from "../test/wire-fixtures/library-list.json";
import maintenanceBackups from "../test/wire-fixtures/maintenance-backups.json";
import modelDownloadRunning from "../test/wire-fixtures/model-download-running.json";
import modelDownloadComplete from "../test/wire-fixtures/model-download-complete.json";
import modelDownloadIdle from "../test/wire-fixtures/model-download-idle.json";
import optionsModels from "../test/wire-fixtures/options-models.json";
import setupModels from "../test/wire-fixtures/setup-models.json";
import setupStatusConfigured from "../test/wire-fixtures/setup-status-configured.json";
import setupStatusReset from "../test/wire-fixtures/setup-status-reset.json";
import setupStatus from "../test/wire-fixtures/setup-status.json";
import sseLog from "../test/wire-fixtures/sse-log.json";
import sseProgress from "../test/wire-fixtures/sse-progress.json";
import sseState from "../test/wire-fixtures/sse-state.json";
import sseStream from "../test/wire-fixtures/sse-stream.json";
import systemCapabilities from "../test/wire-fixtures/system-capabilities.json";
import voices from "../test/wire-fixtures/voices.json";
import type {
  ActiveConversionResponse, ConversionMode, DesktopEventPayload, HealthResponse,
  LibraryBookDetail, LibraryListResponse, MaintenanceBackupsResponse,
  ModelOptionsResponse, RuntimeModelDownloadStatus, RuntimeModelInventory,
  RuntimeSetupStatusResponse, StartConversionResponse, SystemCapabilitiesResponse,
  VoiceOptionsResponse,
} from "./api";

// JSON imports widen string/number literals. Validate only those leaves at runtime;
// all keys, required fields, scalar types, arrays and nullability stay checked by TS.
function oneOf<const T extends string | number>(value: NoInfer<T> extends string ? string : number, choices: readonly T[]): T {
  expect(choices).toContain(value);
  return value as T;
}

// Ignore catch-all signatures when an interface has named fields. Pure records
// (e.g. model group maps) remain open, but their values are checked recursively.
type Named<T> = { [K in keyof T as string extends K ? never : number extends K ? never : K]: T[K] };
type Fields<T> = keyof Named<T> extends never ? T : Named<T>;
type NoExtra<F, T> = unknown extends T ? unknown : F extends readonly (infer Item)[]
  ? NonNullable<T> extends readonly (infer Target)[] ? NoExtra<Item, Target>[] : never
  : F extends object
    ? { [K in keyof F]: K extends keyof Fields<NonNullable<T>>
      ? NoExtra<F[K], Fields<NonNullable<T>>[K]> : never }
    : unknown;

type JsonScalars<T> = T extends string ? string : T extends number ? number
  : T extends object ? { [K in keyof T]: JsonScalars<T[K]> } : T;

function exact<T>() {
  return <F extends T>(value: F & NoExtra<NoInfer<F>, T>): F => value;
}

// Check imports separately before array/union inference can discard variant-only keys.
const fixtures = {
  "health": exact<JsonScalars<HealthResponse>>()(health),
  "library-list": exact<JsonScalars<LibraryListResponse>>()(libraryList),
  "book-detail": exact<JsonScalars<LibraryBookDetail>>()(bookDetail),
  "book-detail-empty": exact<JsonScalars<LibraryBookDetail>>()(bookDetailEmpty),
  "active-idle": exact<JsonScalars<ActiveConversionResponse>>()(activeIdle),
  "active-running": exact<JsonScalars<ActiveConversionResponse>>()(activeRunning),
  "active-completed": exact<JsonScalars<ActiveConversionResponse>>()(activeCompleted),
  "active-cancelled": exact<JsonScalars<ActiveConversionResponse>>()(activeCancelled),
  "conversion-start": exact<JsonScalars<StartConversionResponse>>()(conversionStart),
  "options-models": exact<JsonScalars<ModelOptionsResponse>>()(optionsModels),
  "voices": exact<JsonScalars<VoiceOptionsResponse>>()(voices),
  "system-capabilities": exact<JsonScalars<SystemCapabilitiesResponse>>()(systemCapabilities),
  "setup-status": exact<JsonScalars<RuntimeSetupStatusResponse>>()(setupStatus),
  "setup-status-configured": exact<JsonScalars<RuntimeSetupStatusResponse>>()(setupStatusConfigured),
  "setup-status-reset": exact<JsonScalars<RuntimeSetupStatusResponse>>()(setupStatusReset),
  "setup-models": exact<JsonScalars<RuntimeModelInventory>>()(setupModels),
  "model-download-idle": exact<JsonScalars<RuntimeModelDownloadStatus>>()(modelDownloadIdle),
  "model-download-running": exact<JsonScalars<RuntimeModelDownloadStatus>>()(modelDownloadRunning),
  "model-download-complete": exact<JsonScalars<RuntimeModelDownloadStatus>>()(modelDownloadComplete),
  "maintenance-backups": exact<JsonScalars<MaintenanceBackupsResponse>>()(maintenanceBackups),
  "sse-stream": exact<JsonScalars<DesktopEventPayload>>()(sseStream),
  "sse-progress": exact<JsonScalars<DesktopEventPayload>>()(sseProgress),
  "sse-log": exact<JsonScalars<DesktopEventPayload>>()(sseLog),
  "sse-state": exact<JsonScalars<DesktopEventPayload>>()(sseState),
};

const mode = (value: string): ConversionMode => oneOf(value, ["audiobook", "document_audio"]);
const jobStatus = (value: string) => oneOf(value, [
  "idle", "running", "completed", "failed", "cancelled", "unknown",
]);
const profile = (value: string) => oneOf(value, ["base", "mlx", "cuda"]);

function setupPayload<F extends typeof setupStatus | typeof setupStatusConfigured | typeof setupStatusReset>(fixture: F) {
  return {
    ...fixture,
    schema_version: oneOf(fixture.schema_version, [1]),
    state: oneOf(fixture.state, ["checking", "unconfigured", "error", "restart_required", "ready", "unavailable"]),
    selected_profile: fixture.selected_profile === null ? null : profile(fixture.selected_profile),
    recommended_profile: profile(fixture.recommended_profile),
    profiles: fixture.profiles.map((item) => ({
      ...item,
      id: profile(item.id),
      device: item.device === undefined ? undefined : oneOf(item.device, ["cpu", "cuda"]),
    })),
    selected_model_groups: fixture.selected_model_groups.map((item) => ({
      ...item,
      assets_reason: oneOf(item.assets_reason, ["missing", "incomplete", "corrupt", "unavailable"]),
    })),
    backend: { ...fixture.backend, status: oneOf(fixture.backend.status, ["running"]) },
  } satisfies RuntimeSetupStatusResponse;
}

describe("backend wire payloads", () => {
  it("checks every committed payload fixture", () => {
    const paths = Object.keys(import.meta.glob("../test/wire-fixtures/*.json"));
    expect(paths.map((path) => path.split("/").at(-1)?.replace(/\.json$/, "")).sort())
      .toEqual(Object.keys(fixtures).sort());
  });

  it("rejects unknown enum values and incompatible compile-time contracts", () => {
    expect(() => mode("unsupported")).toThrow();
    // @ts-expect-error A required response field cannot disappear.
    exact<HealthResponse>()({});
    // @ts-expect-error Extra response keys cannot hide behind structural typing.
    exact<HealthResponse>()({ ...health, extra: true });
    // @ts-expect-error JSON primitive types must agree with the declaration.
    exact<HealthResponse>()({ status: 1 });
    // @ts-expect-error A null cannot satisfy a required string.
    exact<HealthResponse>()({ status: null });
    // @ts-expect-error Chapter keys are checked inside arrays too.
    exact<LibraryBookDetail>()({ ...bookDetail, conversion_mode: "audiobook", chapters: [{ ...bookDetail.chapters[0], extra: true }] });
    // @ts-expect-error Named interfaces cannot conceal additions with an index signature.
    exact<ModelOptionsResponse>()({ ...optionsModels, tts_engines: [{ ...optionsModels.tts_engines[0], capabilities: { ...optionsModels.tts_engines[0].capabilities, extra: true } }] });
  });

  it("declares every library field including paginated results and chapter offsets", () => {
    exact<LibraryListResponse>()({
      ...libraryList,
      items: libraryList.items.map((item) => ({ ...item, conversion_mode: mode(item.conversion_mode) })),
    } satisfies LibraryListResponse);
    for (const fixture of [bookDetail, bookDetailEmpty]) {
      exact<LibraryBookDetail>()({ ...fixture, conversion_mode: mode(fixture.conversion_mode) } satisfies LibraryBookDetail);
    }
  });

  it("accepts idle, running and terminal conversion responses", () => {
    exact<ActiveConversionResponse>()(activeIdle satisfies ActiveConversionResponse);
    for (const fixture of [activeRunning, activeCompleted, activeCancelled]) {
      exact<ActiveConversionResponse>()({
        ...fixture, job: { ...fixture.job, status: jobStatus(fixture.job.status) },
      } satisfies ActiveConversionResponse);
    }
    exact<StartConversionResponse>()({
      ...conversionStart, status: jobStatus(conversionStart.status),
    } satisfies StartConversionResponse);
  });

  it("declares model, voice and capability response fields", () => {
    exact<ModelOptionsResponse>()(optionsModels satisfies ModelOptionsResponse);
    exact<VoiceOptionsResponse>()(voices satisfies VoiceOptionsResponse);
    exact<SystemCapabilitiesResponse>()({
      ...systemCapabilities,
      runtime_probe: {
        ...systemCapabilities.runtime_probe,
        state: oneOf(systemCapabilities.runtime_probe.state, ["fresh", "stale", "pending"]),
      },
    } satisfies SystemCapabilitiesResponse);
  });

  it("accepts configured, unconfigured and reset setup responses", () => {
    exact<RuntimeSetupStatusResponse>()(setupPayload(setupStatus));
    exact<RuntimeSetupStatusResponse>()(setupPayload(setupStatusConfigured));
    exact<RuntimeSetupStatusResponse>()(setupPayload(setupStatusReset));
    exact<RuntimeModelInventory>()(setupModels satisfies RuntimeModelInventory);
    for (const fixture of [modelDownloadIdle, modelDownloadRunning, modelDownloadComplete]) {
      exact<RuntimeModelDownloadStatus>()({
        ...fixture,
        lifecycle: oneOf(fixture.lifecycle, ["idle", "running", "stopping", "cancelled", "complete", "failed"]),
      } satisfies RuntimeModelDownloadStatus);
    }
  });

  it("declares backup metadata and health fields", () => {
    exact<MaintenanceBackupsResponse>()(maintenanceBackups satisfies MaintenanceBackupsResponse);
    exact<HealthResponse>()(health satisfies HealthResponse);
  });

  it("accepts stream identity, progress, unscoped logs and state event payloads", () => {
    exact<DesktopEventPayload>()(sseStream satisfies DesktopEventPayload);
    exact<DesktopEventPayload>()(sseProgress satisfies DesktopEventPayload);
    exact<DesktopEventPayload>()(sseLog satisfies DesktopEventPayload);
    exact<DesktopEventPayload>()(sseState satisfies DesktopEventPayload);
    expect(sseProgress.backend_instance_id).toBe(sseStream.backend_instance_id);
    expect([sseProgress.event_sequence, sseLog.event_sequence, sseState.event_sequence]).toEqual([1, 2, 3]);
  });
});

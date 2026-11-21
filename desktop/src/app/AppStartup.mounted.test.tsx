// @vitest-environment happy-dom
import { act, type MutableRefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import type { ConversionJobController } from "../features/conversion/useConversionJob";
import type { RuntimeSetupStatusResponse } from "../types/api";
import { rememberReadyRuntime } from "./runtimeStartup";

const mocks = vi.hoisted(() => ({
  apiClient: { getRuntimeSetupStatus: vi.fn() },
  submit: vi.fn(async () => ({ ok: true, jobId: "job" })),
}));
vi.mock("./useBackendClient", () => ({ useBackendClient: () => ({
  apiClient: mocks.apiClient, backendBaseUrl: "http://runtime.test", connectionState: "connected",
}) }));
vi.mock("./useRuntimeBootstrapRefresh", () => ({ useRuntimeBootstrapRefresh: vi.fn() }));
vi.mock("./useGlobalShortcuts", () => ({ useGlobalShortcuts: vi.fn() }));
vi.mock("../features/library/useLibrary", () => ({ useLibrary: () => ({ items: [], unfilteredItems: [], refreshLibrary: vi.fn() }) }));
vi.mock("../features/player/useAudioPlayer", () => ({ useAudioPlayer: () => ({ audioProps: {}, audioRef: { current: null }, sleepTimer: { kind: "off" }, sleepRemainingSec: null, setSleepTimer: vi.fn() }) }));
vi.mock("../features/conversion/useConversionJob", () => ({ useConversionJob: () => ({
  uiState: "idle", startConversion: mocks.submit,
}) }));
vi.mock("../features/conversion/ConversionPanel", () => ({ ConversionPanel: (props: {
  conversionJob: ConversionJobController; startCommandRef: MutableRefObject<(() => void) | null>;
}) => {
  const start = () => { void props.conversionJob.startConversion({ source_path: "/book.txt", conversion_mode: "audiobook" }); };
  props.startCommandRef.current = start;
  return <button onClick={start}>Submit conversion</button>;
} }));
vi.mock("../features/setup/SettingsPage", () => ({ SettingsPage: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("../features/setup/SetupPage", () => ({ SetupPage: () => <h1>Runtime setup</h1> }));
vi.mock("../features/library/LibraryPanel", () => ({ LibraryPanel: () => null }));
vi.mock("../features/library/LibraryMaintenanceSection", () => ({ LibraryMaintenanceSection: () => null }));
vi.mock("../features/logs/LogPanel", () => ({ LogPanel: () => null }));
vi.mock("../features/player/PlayerPanel", () => ({ PlayerPanel: () => null }));
vi.mock("../features/progress/ProgressPanel", () => ({ ProgressPanel: () => null }));

import App from "../App";

const ready: RuntimeSetupStatusResponse = {
  schema_version: 1, configured: true, state: "ready", state_error: null,
  selected_profile: "cuda", selected_model_groups: [
    { id: "tts-cpu-cuda", ready: true, runtime_ready: true, assets_ready: true, assets_reason: null },
  ], packages: { ready: true, missing: [], incompatible: [] }, probe: { ready: true, checks: [] },
  ready: true, restart_required: false, profiles: [],
  backend: { instance_id: "new-backend", status: "running" },
};

let container: HTMLDivElement;
let root: Root;
let resolveStatus: (status: RuntimeSetupStatusResponse) => void;

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
  vi.useFakeTimers();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  mocks.apiClient.getRuntimeSetupStatus.mockImplementation(() => new Promise((resolve) => { resolveStatus = resolve; }));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.useRealTimers();
});

async function click(text: string) {
  const button = [...container.querySelectorAll("button")].find((item) => item.textContent === text);
  expect(button).toBeDefined();
  await act(async () => button!.click());
}

it("shows the returning dashboard without a wizard flash and fences starts until confirmation", async () => {
  rememberReadyRuntime(ready);
  await act(async () => root.render(<App />));
  expect(container.querySelector('main.app-frame')).not.toBeNull();
  expect(container.textContent).not.toContain("Runtime setup");
  await click("Create");
  await click("Submit conversion");
  expect(mocks.submit).not.toHaveBeenCalled();
  await act(async () => resolveStatus({ ...ready, ready: false, state: "checking" }));
  expect(container.querySelector('main.app-frame')).not.toBeNull();
  expect(container.querySelector(".setup-startup")).toBeNull();
  expect(container.textContent).not.toContain("Runtime setup");
  await click("Submit conversion");
  expect(mocks.submit).not.toHaveBeenCalled();
  await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
  await act(async () => resolveStatus(ready));
  expect(container.textContent).not.toContain("Runtime setup");
  await click("Submit conversion");
  expect(mocks.submit).toHaveBeenCalledTimes(1);
});

it("opens setup when the current launch definitively fails verification", async () => {
  rememberReadyRuntime(ready);
  await act(async () => root.render(<App />));
  await act(async () => resolveStatus({ ...ready, ready: false, state: "unavailable" }));
  expect(container.querySelector("h1")?.textContent).toBe("Runtime setup");
  expect(mocks.submit).not.toHaveBeenCalled();
});

it("keeps the first launch on the startup gate until its runtime is checked", async () => {
  await act(async () => root.render(<App />));
  expect(container.querySelector(".setup-startup")).not.toBeNull();
  expect(container.querySelector('main.app-frame')).toBeNull();
  await act(async () => resolveStatus(ready));
  expect(container.querySelector(".setup-startup")).toBeNull();
  expect(container.querySelector('main.app-frame')).not.toBeNull();
});

it("offers Retry after background status errors exhaust their polling budget", async () => {
  rememberReadyRuntime(ready);
  mocks.apiClient.getRuntimeSetupStatus.mockRejectedValue(new Error("offline"));
  await act(async () => root.render(<App />));
  await act(async () => vi.advanceTimersByTimeAsync(120_000));
  expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(61);
  expect(container.querySelector("main.app-frame")).not.toBeNull();
  expect(container.textContent).toContain("Runtime check needs more time");
  await act(async () => vi.advanceTimersByTimeAsync(10_000));
  expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(61);
  mocks.apiClient.getRuntimeSetupStatus.mockResolvedValue(ready);
  await click("Retry");
  expect(mocks.apiClient.getRuntimeSetupStatus).toHaveBeenCalledTimes(62);
  expect(container.querySelector(".runtime-check-notice")).toBeNull();
  await click("Create");
  await click("Submit conversion");
  expect(mocks.submit).toHaveBeenCalledOnce();
});

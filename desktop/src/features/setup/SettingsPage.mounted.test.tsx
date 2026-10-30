// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { RequestJson } from "../../api/http";
import { createSystemApi } from "../../api/resources/system";
import { conversionPanelViewModel } from "../../test/conversionPanelViewModel";
import { CleanerOptionsView } from "../conversion/views/CleanerOptionsView";
import { SettingsPage } from "./SettingsPage";

it("ignores an old folder failure after leaving and reopening Settings", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  let rejectOpen!: (error: Error) => void;
  const requestJson = vi.fn((path: string) => path.endsWith("/open")
    ? new Promise((_, reject) => { rejectOpen = reject; })
    : Promise.resolve({ output_path: "C:/scratch/output", app_data_path: "C:/scratch" }));
  const client = { requestJson, ...createSystemApi(requestJson as unknown as RequestJson) } as unknown as ApiClient;
  const container = document.createElement("div");
  const root = createRoot(container);
  const render = (active: boolean) => root.render(<SettingsPage active={active} apiClient={client}>{null}</SettingsPage>);
  try {
    await act(async () => render(true));
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Open output folder"]')!.click());
    await act(async () => render(false));
    await act(async () => render(true));
    await act(async () => rejectOpen(new Error("Old open failed")));
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(container.textContent).toContain("C:/scratch/output");
    expect(container.querySelector<HTMLButtonElement>('[aria-label="Open output folder"]')!.disabled).toBe(false);
  } finally {
    await act(async () => root.unmount());
    vi.unstubAllGlobals();
  }
});

function cleanerViewModel() {
  const model = conversionPanelViewModel();
  return {
    ...model,
    cleanerOptions: {
      ...model.cleanerOptions,
      cleanerBackend: "cuda",
      cleanerBackendOptions: [{ value: "cuda", label: "Local CUDA" }],
      cleanerRuntimeStatus: "CUDA runtime available.",
      showCleanerCudaControls: true,
      cleanerCudaDevice: "cuda",
      cleanerCudaQuantization: "4bit",
    },
  };
}

// The backend reports the log path only when the log exists (browser-started backends have none).
it.each([
  ["C:/Orpheus", "C:/Orpheus/data/logs/backend.log"],
  ["/Users/listener/Orpheus/", null],
  ["C:\\Orpheus\\", "C:\\Orpheus\\data\\logs\\backend.log"],
] as const)("shows models in place, library tools, storage, version and opens only the selected storage folder (%s)", async (appDataPath, logPath) => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const requestJson = vi.fn(async () => ({ output_path: "C:/books", app_data_path: appDataPath, log_path: logPath }));
  const client = { requestJson, ...createSystemApi(requestJson as unknown as RequestJson) } as unknown as ApiClient;
  const models = (
    <section className="card settings-section" aria-label="Voice & models">
      <div className="card-header"><h2 className="card-title">Voice &amp; models</h2></div>
      <p>Models on this device</p>
    </section>
  );
  const model = cleanerViewModel();
  try {
    await act(async () => root.render(
      <SettingsPage active apiClient={client} models={models} maintenance={<p>Maintenance tools</p>}>
        <CleanerOptionsView {...model} hidden={false} />
      </SettingsPage>,
    ));
    expect([...container.querySelectorAll(".settings-page > .card > .card-header .card-title")].map((heading) => heading.textContent))
      .toEqual(["Voice & models", "Text cleaning", "Library", "Storage", "About"]);
    expect(container.textContent).toContain("Models on this device");
    expect(container.querySelector('[aria-label="Library maintenance"]')?.textContent).toContain("Maintenance tools");
    expect(container.textContent).toContain("C:/books");
    expect(container.textContent).toContain(appDataPath);
    const logs = [...container.querySelectorAll(".settings-storage-row")].find((row) => row.querySelector(".field-label")?.textContent === "Logs");
    expect(logs?.querySelector(".settings-path")?.textContent).toBe(logPath ?? undefined);
    expect([...container.querySelectorAll(".settings-storage-row .settings-path")].map((path) => path.getAttribute("data-tip")))
      .toEqual(logPath ? ["C:/books", appDataPath, logPath] : ["C:/books", appDataPath]);
    expect(logs?.querySelector("button, input") ?? null).toBeNull();
    expect(container.textContent).toContain("Orpheus 1.0.0");
    await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="Open output folder"]')!.click());
    expect(requestJson).toHaveBeenCalledWith("/api/system/storage/output/open", { timeoutMs: 8_000, method: "POST" });
  } finally {
    await act(async () => root.unmount());
    container.remove();
    localStorage.clear();
  }
});

it("shows the device as text until changed", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const model = cleanerViewModel();
  const setDevice = vi.fn();
  model.actions.setCleanerCudaDevice = setDevice;
  try {
    await act(async () => root.render(<SettingsPage active apiClient={null}><CleanerOptionsView {...model} hidden={false} /></SettingsPage>));
    expect(container.querySelector("#cleaner-cuda-device-input")).toBeNull();
    expect(container.querySelector("#cleaner-cuda-device")?.textContent).toBe("CUDA");
    await act(async () => container.querySelector<HTMLButtonElement>("#cleaner-cuda-device-change")!.click());
    const device = container.querySelector<HTMLInputElement>("#cleaner-cuda-device-input")!;
    expect(device.value).toBe("CUDA");
    expect(device.getAttribute("spellcheck")).toBe("false");
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    await act(async () => {
      setValue.call(device, "CUDA:1");
      device.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(setDevice).toHaveBeenCalledWith("cuda:1");
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.restoreAllMocks();
  }
});

it("keeps the cleaner mounted and hidden between Settings visits, with its status in the header", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const model = cleanerViewModel();
  const render = (active: boolean) => root.render(<SettingsPage active={active} apiClient={null}><CleanerOptionsView {...model} hidden={!active} /></SettingsPage>);
  try {
    await act(async () => render(true));
    const cleaner = container.querySelector<HTMLElement>("#tab-config")!;
    const device = container.querySelector<HTMLElement>("#cleaner-cuda-device")!;
    expect(device.textContent).toBe("CUDA");
    // A working runtime is announced but not drawn as a badge.
    const status = cleaner.querySelector("#cleaner-runtime-status")!;
    expect(status.getAttribute("role")).toBe("status");
    expect(status.classList.contains("sr-only")).toBe(true);
    expect(cleaner.querySelector(".settings-section-hint")?.textContent).toBe("Tidies text before narration.");
    expect([...cleaner.querySelectorAll(".conversion-form-grid > .field")].map((field) => field.querySelector(".field-label")?.textContent)).toEqual(["Cleaner", "Model", "Device"]);
    await act(async () => render(false));
    expect(container.querySelector(".settings-content > #tab-config")).toBe(cleaner);
    expect(cleaner.hidden).toBe(true);
    expect(container.querySelector(".settings-page")).toBeNull();
    await act(async () => render(true));
    expect(container.querySelector(".settings-page > #tab-config")).toBe(cleaner);
    expect(container.querySelector("#cleaner-cuda-device")).toBe(device);
    expect(cleaner.hidden).toBe(false);
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

it("keeps runtime retry and cloud credentials inside the cleaner card", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const model = cleanerViewModel();
  model.cleanerOptions.requiresApiKey = true;
  model.cleanerOptions.cleanerBackend = "api";
  model.cleanerOptions.cleanerBackendOptions = [{ value: "api", label: "Cloud API" }];
  model.cleanerOptions.cleanerRuntimeStatus = "Cloud API selected. Paste an API key to load models.";
  model.cleanerOptions.showCleanerCudaControls = false;
  model.cleanerOptions.selectedBackendDisplayName = "Cloud API";
  model.capabilities.canRetryRuntimeOptions = true;
  model.capabilities.canFetchCleanerModels = true;
  const retry = vi.fn();
  const fetchModels = vi.fn(async () => {});
  model.actions.onRetryRuntimeOptions = retry;
  model.actions.onFetchCleanerModels = fetchModels;
  try {
    await act(async () => root.render(<SettingsPage active apiClient={null}><CleanerOptionsView {...model} hidden={false} /></SettingsPage>));
    const cleaner = container.querySelector("#tab-config.card")!;
    expect(cleaner.querySelector("#cleaner-api-key-input")?.getAttribute("aria-label")).toBe("Cloud API API key");
    expect(cleaner.querySelector("#cleaner-model-fetch-status")).not.toBeNull();
    expect(cleaner.querySelector("#cleaner-cuda-device-input")).toBeNull();
    await act(async () => cleaner.querySelector<HTMLButtonElement>(".settings-runtime-notice button")!.click());
    expect(retry).toHaveBeenCalledOnce();
    await act(async () => cleaner.querySelector<HTMLButtonElement>("#fetch-cleaner-models-btn")!.click());
    expect(fetchModels).toHaveBeenCalledOnce();
  } finally {
    await act(async () => root.unmount());
    container.remove();
  }
});

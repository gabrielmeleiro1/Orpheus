import { useState } from "react";

import { ConversionHelp } from "./ConversionHelp";
import { TermSelect } from "../TermSelect";
import { formatDevice } from "../../../format/runtime";
import type {
  CleanerOptionsViewModel,
  ConversionCapabilities,
  ConversionPanelActions,
} from "../conversionPanelStateTypes";

interface CleanerOptionsViewProps {
  cleanerOptions: CleanerOptionsViewModel;
  capabilities: ConversionCapabilities;
  actions: ConversionPanelActions;
  hidden: boolean;
}

type RuntimeHealth = "ok" | "busy" | "error" | "unknown";

function runtimeHealth(runtimeStatusClass: string, statusText: string): RuntimeHealth {
  if (runtimeStatusClass.includes("is-error")) {
    return "error";
  }
  const lower = statusText.toLowerCase();
  if (lower.includes("unavailable") || lower.includes("not available")) {
    return "error";
  }
  if (lower.includes("ready") || lower.includes("available")) {
    return "ok";
  }
  if (lower.includes("check") || lower.includes("load") || lower.includes("…") || lower.includes("...")) {
    return "busy";
  }
  return "unknown";
}

export function CleanerOptionsView({
  cleanerOptions,
  capabilities,
  actions,
  hidden,
}: CleanerOptionsViewProps) {
  const health = runtimeHealth(cleanerOptions.runtimeStatusClass, cleanerOptions.cleanerRuntimeStatus);
  // A working runtime needs no badge; its status is still announced.
  const statusClass = health === "ok" ? "sr-only" : `field-hint cleaner-runtime-status${health === "error" ? " is-error" : ""}`;
  const device = formatDevice(cleanerOptions.cleanerCudaDevice.trim() || cleanerOptions.cleanerCudaDevicePlaceholder);
  const [editingDevice, setEditingDevice] = useState(false);

  return (
    <section id="tab-config" className="card config-panel settings-section" role="tabpanel" aria-label="Configuration" aria-labelledby="tab-btn-settings" hidden={hidden}>
      <div className="card-header">
        <h2 className="card-title">Text cleaning</h2>
        <p className="settings-section-hint">Tidies text before narration.</p>
      </div>
      <div className="config-layout">
        <p id="cleaner-runtime-status" className={statusClass} role="status" aria-atomic="true">
          {cleanerOptions.cleanerRuntimeStatus}
        </p>
        {capabilities.canRetryRuntimeOptions ? (
          <div className="settings-runtime-notice" role="status">
            <p>Options could not load.</p>
            <button className="btn btn-ghost" type="button" onClick={actions.onRetryRuntimeOptions}>
              Retry
            </button>
          </div>
        ) : null}
        <div className="conversion-section">
          <div className="form-grid conversion-form-grid">
            <div className="field">
              <label className="field-label" htmlFor="cleaner-backend-select">Cleaner</label>
              <TermSelect
                id="cleaner-backend-select"
                label="Cleaner backend"
                value={cleanerOptions.cleanerBackend}
                options={cleanerOptions.cleanerBackendOptions}
                placeholder="Select backend"
                onChange={actions.setCleanerBackend}
              />
            </div>

            <div className="field">
              <label className="field-label" htmlFor="cleaner-model-select">Model</label>
              <TermSelect
                id="cleaner-model-select"
                label="Cleaner model"
                value={cleanerOptions.cleanerModel}
                options={cleanerOptions.cleanerModelOptions}
                placeholder={cleanerOptions.requiresApiKey ? "Paste an API key to load models" : cleanerOptions.cleanerBackend === "basic" ? "No model required" : "-"}
                disabled={cleanerOptions.cleanerModelOptions.length === 0}
                onChange={actions.setCleanerModel}
              />
            </div>

            {cleanerOptions.showCleanerCudaControls ? (
              // Precision is the "Cleaner precision" setting above (the same saved
              // default), so only the GPU is chosen here. It reads as text until changed.
              <div className="field cleaner-device-field">
                <div className="conversion-label-row">
                  <span className="field-label" id="cleaner-cuda-device-label">Device</span>
                  <ConversionHelp label="Device help" text="CUDA picks the GPU automatically; CUDA:0 is the first NVIDIA GPU." />
                </div>
                {editingDevice ? (
                  <input
                    id="cleaner-cuda-device-input"
                    className="field-input"
                    type="text"
                    spellCheck={false}
                    autoCapitalize="none"
                    autoCorrect="off"
                    autoFocus
                    aria-label="cleaner CUDA device"
                    placeholder={formatDevice(cleanerOptions.cleanerCudaDevicePlaceholder)}
                    value={device}
                    onChange={(event) => actions.setCleanerCudaDevice(event.target.value.toLowerCase())}
                    onBlur={() => setEditingDevice(false)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === "Escape") setEditingDevice(false);
                    }}
                  />
                ) : (
                  <div className="cleaner-device-value" role="group" aria-labelledby="cleaner-cuda-device-label">
                    <span id="cleaner-cuda-device">{device}</span>
                    <button
                      id="cleaner-cuda-device-change"
                      className="btn btn-ghost btn-sm btn-flush-end"
                      type="button"
                      aria-label="Change cleaner device"
                      onClick={() => setEditingDevice(true)}
                    >
                      Change
                    </button>
                  </div>
                )}
              </div>
            ) : null}
          </div>
        </div>

        {cleanerOptions.requiresApiKey ? (
          <section className="conversion-section settings-api-section">
            <div className="conversion-section-header">
              <h3 className="conversion-group-heading">{cleanerOptions.selectedBackendDisplayName} cleaner</h3>
            </div>
            <div className="form-grid conversion-form-grid">
              <div className="field">
                <div className="conversion-label-row">
                  <label className="field-label" htmlFor="cleaner-api-key-input">API key</label>
                  <ConversionHelp label="API key help" text="Kept for this session only and sent only to this provider. Re-enter it after reconnecting." />
                </div>
                <input
                  id="cleaner-api-key-input"
                  className="field-input"
                  type="password"
                  aria-label={`${cleanerOptions.selectedBackendDisplayName} API key`}
                  autoComplete="off"
                  placeholder="sk-…"
                  value={cleanerOptions.cleanerApiKey}
                  onChange={(event) => actions.setCleanerApiKey(event.target.value)}
                />
              </div>
              <div className="field">
                <span className="field-label">Models</span>
                <div className="action-row">
                  <button
                    id="fetch-cleaner-models-btn"
                    className="btn btn-secondary"
                    type="button"
                    disabled={!capabilities.canFetchCleanerModels}
                    data-tip="List the models available to this API key"
                    onClick={() => void actions.onFetchCleanerModels()}
                  >
                    {cleanerOptions.openAiModelFetchLoading ? "Fetching…" : "Fetch models"}
                  </button>
                </div>
                <span
                  id="cleaner-model-fetch-status"
                  className={`field-hint${cleanerOptions.openAiModelFetchError ? " is-error" : ""}`}
                >
                  {cleanerOptions.openAiModelFetchStatus}
                </span>
              </div>
            </div>
          </section>
        ) : null}
      </div>
    </section>
  );
}

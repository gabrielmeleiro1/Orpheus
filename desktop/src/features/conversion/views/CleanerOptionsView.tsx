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

function statusDotClass(runtimeStatusClass: string, statusText: string): string {
  if (runtimeStatusClass.includes("is-error")) {
    return "status-dot is-error";
  }
  const lower = statusText.toLowerCase();
  if (lower.includes("ready") || lower.includes("available")) {
    return "status-dot is-ok";
  }
  if (lower.includes("check") || lower.includes("load") || lower.includes("…") || lower.includes("...")) {
    return "status-dot is-busy";
  }
  return "status-dot";
}

export function CleanerOptionsView({
  cleanerOptions,
  capabilities,
  actions,
  hidden,
}: CleanerOptionsViewProps) {
  return (
    <section id="tab-config" className="panel config-panel" role="tabpanel" aria-label="Configuration" aria-labelledby="tab-btn-settings" hidden={hidden}>
      <div className="config-layout">
        <section className="conversion-section">
          <div className="conversion-section-header conversion-section-header-row">
            <div>
              <h2
                className="conversion-group-heading"
                data-tip="The cleaner prepares text before narration. It is independent of the TTS engine — choosing Cloud API here does not change the voice."
                data-tip-pos="bottom"
              >
                Cleaner runtime
              </h2>
              <p className="conversion-section-hint">Backend, model, and device used to prepare text before narration.</p>
            </div>
            <button
              id="runtime-setup-btn"
              className="btn btn-accent-soft"
              type="button"
              data-tip="Install or repair the on-device cleaner and TTS runtimes"
              onClick={actions.onOpenRuntimeSetup}
            >
              Runtime setup
            </button>
          </div>

          <div className="form-grid conversion-form-grid">
            <div className="field">
              <label className="field-label" htmlFor="cleaner-backend-select">Backend</label>
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
              <span className="field-label" id="cleaner-status-label">Status</span>
              <div
                id="cleaner-runtime-status"
                className={`runtime-status-pill ${cleanerOptions.runtimeStatusClass}`}
                role="status"
                aria-labelledby="cleaner-status-label"
              >
                <span className={statusDotClass(cleanerOptions.runtimeStatusClass, cleanerOptions.cleanerRuntimeStatus)} aria-hidden="true" />
                <span>{cleanerOptions.cleanerRuntimeStatus}</span>
              </div>
            </div>

            <div className="field">
              <label className="field-label" htmlFor="cleaner-model-select">Model</label>
              <TermSelect
                id="cleaner-model-select"
                label="Cleaner model"
                value={cleanerOptions.cleanerModel}
                options={cleanerOptions.cleanerModelOptions}
                placeholder={cleanerOptions.requiresApiKey ? "Paste an API key to load models" : "-"}
                disabled={cleanerOptions.cleanerModelOptions.length === 0}
                onChange={actions.setCleanerModel}
              />
            </div>

            {cleanerOptions.showCleanerCudaControls ? (
              <>
                <div className="field">
                  <label
                    className="field-label"
                    htmlFor="cleaner-cuda-device-input"
                    data-tip="Use CUDA for automatic placement, or CUDA:0 for the first NVIDIA GPU."
                    data-tip-pos="right"
                  >
                    Device
                  </label>
                  <input
                    id="cleaner-cuda-device-input"
                    className="field-input"
                    type="text"
                    spellCheck={false}
                    autoCapitalize="none"
                    autoCorrect="off"
                    aria-label="cleaner CUDA device"
                    placeholder={formatDevice(cleanerOptions.cleanerCudaDevicePlaceholder)}
                    value={formatDevice(cleanerOptions.cleanerCudaDevice)}
                    onChange={(event) => actions.setCleanerCudaDevice(event.target.value.toLowerCase())}
                  />
                </div>

                {cleanerOptions.cleanerCudaQuantizationOptions.length > 0 ? (
                  <div className="field">
                    <label
                      className="field-label"
                      htmlFor="cleaner-cuda-quantization-select"
                      data-tip="4bit uses the least GPU memory. 8bit and fp16 use more memory; speed depends on your GPU."
                      data-tip-pos="right"
                    >
                      Quantization
                    </label>
                    <TermSelect
                      id="cleaner-cuda-quantization-select"
                      label="Cleaner CUDA quantization"
                      value={cleanerOptions.cleanerCudaQuantization}
                      options={cleanerOptions.cleanerCudaQuantizationOptions}
                      placeholder="default"
                      onChange={actions.setCleanerCudaQuantization}
                    />
                  </div>
                ) : null}
              </>
            ) : null}
          </div>
        </section>

        {cleanerOptions.requiresApiKey ? (
          <section className="conversion-section">
            <div className="conversion-section-header">
              <h2 className="conversion-group-heading">{cleanerOptions.selectedBackendDisplayName} cleaner</h2>
              <p className="conversion-section-hint">API credentials for this session only.</p>
            </div>
            <div className="form-grid conversion-form-grid">
              <div className="field">
                <label
                  className="field-label"
                  htmlFor="cleaner-api-key-input"
                  data-tip="Kept for this session only and sent only to this provider. Re-enter it after reconnecting."
                  data-tip-pos="right"
                >
                  API key
                </label>
                <input
                  id="cleaner-api-key-input"
                  className="field-input"
                  type="password"
                  aria-label={`${cleanerOptions.selectedBackendDisplayName} API key`}
                  autoComplete="off"
                  placeholder="sk-..."
                  value={cleanerOptions.cleanerApiKey}
                  onChange={(event) => actions.setCleanerApiKey(event.target.value)}
                />
              </div>
              <div className="field">
                <span className="field-label">Models</span>
                <div className="action-row">
                  <button
                    id="fetch-cleaner-models-btn"
                    className="btn"
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

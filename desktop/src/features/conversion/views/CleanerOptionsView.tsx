import { TermSelect } from "../TermSelect";
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

export function CleanerOptionsView({
  cleanerOptions,
  capabilities,
  actions,
  hidden,
}: CleanerOptionsViewProps) {
  return (
    <section id="tab-config" className="panel config-panel" role="tabpanel" aria-label="Configuration" aria-labelledby="tab-btn-settings" hidden={hidden}>
      <header className="conversion-context">
        <p className="conversion-lede">Choose how Orpheus prepares text before narration.</p>
        <span className="conversion-header-meta">Runtime</span>
      </header>
      <div className="config-grid">
        <div className="panel-section config-panel-section">
          <div className="section-title">
            <span
              data-tip="The cleaner prepares text before narration. It is independent of the TTS engine — choosing Cloud API here does not change the voice."
              data-tip-pos="bottom"
            >
              Cleaner runtime
            </span>
            <button
              id="runtime-setup-btn"
              className="btn btn-primary"
              type="button"
              data-tip="Install or repair the on-device cleaner and TTS runtimes"
              onClick={actions.onOpenRuntimeSetup}
            >
              Runtime setup
            </button>
          </div>
          <div className="form-row">
            <span className="form-row-label">Backend</span>
            <div className="form-row-field">
              <TermSelect
                id="cleaner-backend-select"
                label="Cleaner backend"
                value={cleanerOptions.cleanerBackend}
                options={cleanerOptions.cleanerBackendOptions}
                placeholder="Select backend"
                onChange={actions.setCleanerBackend}
              />
            </div>
          </div>

          <div className="form-row">
            <span className="form-row-label">Status</span>
            <div className="form-row-field">
              <span id="cleaner-runtime-status" className={cleanerOptions.runtimeStatusClass}>
                {cleanerOptions.cleanerRuntimeStatus}
              </span>
            </div>
          </div>

          <div className="form-row">
            <span className="form-row-label">Model</span>
            <div className="form-row-field">
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
          </div>

          {cleanerOptions.showCleanerCudaControls ? (
            <>
              <div className="form-row">
                <span className="form-row-label" data-tip="CUDA device the cleaner model runs on, e.g. cuda:0" data-tip-pos="right">Device</span>
                <div className="form-row-field">
                  <input
                    id="cleaner-cuda-device-input"
                    className="field-input"
                    type="text"
                    aria-label="cleaner CUDA device"
                    placeholder={cleanerOptions.cleanerCudaDevicePlaceholder}
                    value={cleanerOptions.cleanerCudaDevice}
                    onChange={(event) => actions.setCleanerCudaDevice(event.target.value)}
                  />
                </div>
              </div>

              {cleanerOptions.cleanerCudaQuantizationOptions.length > 0 ? (
                <div className="form-row">
                  <span
                    className="form-row-label"
                    data-tip="Model precision. Lower bits run faster with a slight quality trade-off."
                    data-tip-pos="right"
                  >
                    Quantization
                  </span>
                  <div className="form-row-field">
                    <TermSelect
                      id="cleaner-cuda-quantization-select"
                      label="Cleaner CUDA quantization"
                      value={cleanerOptions.cleanerCudaQuantization}
                      options={cleanerOptions.cleanerCudaQuantizationOptions}
                      placeholder="default"
                      onChange={actions.setCleanerCudaQuantization}
                    />
                  </div>
                </div>
              ) : null}
            </>
          ) : null}
        </div>

        {cleanerOptions.requiresApiKey ? (
          <div className="panel-section config-panel-section">
            <div className="section-title">
              <span>{cleanerOptions.selectedBackendDisplayName} cleaner</span>
            </div>
            <div className="form-row">
              <span
                className="form-row-label"
                data-tip="Kept for this session only and sent only to this provider. Re-enter it after reconnecting."
                data-tip-pos="right"
              >
                API key
              </span>
              <div className="form-row-field">
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
            </div>

            <div className="action-row">
              <button
                id="fetch-cleaner-models-btn"
                className="btn btn-compact"
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
        ) : null}
      </div>
    </section>
  );
}

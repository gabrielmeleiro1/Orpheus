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
    <section id="tab-config" className="panel config-panel" role="tabpanel" aria-label="Configuration" hidden={hidden}>
      <div className="panel-header">
        <span>configuration</span>
      </div>

      <p className="field-hint config-runtime-copy">
        TTS runtime synthesizes speech (shown in the header as TTS). Cleaner runtime prepares text before
        synthesis and is chosen independently — selecting Cloud API here does not change the TTS engine.
      </p>

      <div className="config-grid">
        <div className="panel-section config-panel-section">
          <div className="section-title">
            <span>cleaner runtime</span>
          </div>
          <div className="form-row">
            <span className="form-row-label">backend</span>
            <div className="form-row-field">
              <TermSelect
                id="cleaner-backend-select"
                label="cleaner backend"
                value={cleanerOptions.cleanerBackend}
                options={cleanerOptions.cleanerBackendOptions}
                placeholder="select backend"
                onChange={actions.setCleanerBackend}
              />
            </div>
          </div>

          <div className="form-row">
            <span className="form-row-label">status</span>
            <div className="form-row-field">
              <span id="cleaner-runtime-status" className={cleanerOptions.runtimeStatusClass}>
                {cleanerOptions.cleanerRuntimeStatus}
              </span>
            </div>
          </div>

          <div className="form-row">
            <span className="form-row-label">model</span>
            <div className="form-row-field">
              <TermSelect
                id="cleaner-model-select"
                label="cleaner model"
                value={cleanerOptions.cleanerModel}
                options={cleanerOptions.cleanerModelOptions}
                placeholder="-"
                disabled={cleanerOptions.cleanerModelOptions.length === 0}
                onChange={actions.setCleanerModel}
              />
            </div>
          </div>

          {cleanerOptions.showCleanerCudaControls ? (
            <>
              <div className="form-row">
                <span className="form-row-label">device</span>
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
                  <span className="form-row-label">quantization</span>
                  <div className="form-row-field">
                    <TermSelect
                      id="cleaner-cuda-quantization-select"
                      label="cleaner cuda quantization"
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
              <span className="form-row-label">api key</span>
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
                onClick={() => void actions.onFetchCleanerModels()}
              >
                {cleanerOptions.openAiModelFetchLoading ? "fetching..." : "fetch models"}
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

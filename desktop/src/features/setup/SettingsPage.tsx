import { useEffect, useState, type ReactNode } from "react";
import type { ApiClient } from "../../api/client";
import { createSystemApi } from "../../api/resources/system";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { version } from "../../../package.json";
import { DESKTOP_SETTINGS_CHANGED_EVENT, readPersistedSettings } from "../conversion/conversionOptionsShared";
import { MODEL_GROUP_DETAILS } from "./setupModel";
import { safeError } from "./setupPageHelpers";
import "./settings.css";

export function SettingsPage({
  active,
  apiClient,
  status,
  onManageModels,
  children,
}: {
  active: boolean;
  apiClient: ApiClient | null;
  status: RuntimeSetupStatusResponse | null;
  onManageModels: () => void;
  children: ReactNode;
}) {
  const [settings, setSettings] = useState(readPersistedSettings);
  const [folders, setFolders] = useState<{ output_path: string; app_data_path: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  useEffect(() => {
    const refresh = () => setSettings(readPersistedSettings());
    window.addEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, refresh);
    return () => window.removeEventListener(DESKTOP_SETTINGS_CHANGED_EVENT, refresh);
  }, []);

  useEffect(() => {
    if (!active || !apiClient) return;
    let current = true;
    setFolders(null);
    setError(null);
    const system = createSystemApi(apiClient.requestJson);
    void Promise.resolve()
      .then(() => system.getStorageFolders({ timeoutMs: 8_000 }))
      .then((next) => {
        if (current) setFolders(next);
      })
      .catch(() => {
        if (current) setError("Storage paths could not be loaded. Reopen Settings to retry.");
      });
    return () => {
      current = false;
    };
  }, [active, apiClient]);

  const openFolder = async (folder: "output" | "app_data") => {
    if (!apiClient || opening) return;
    setOpening(folder);
    setError(null);
    try {
      await createSystemApi(apiClient.requestJson).openStorageFolder(folder, { timeoutMs: 8_000 });
    } catch (reason) {
      setError(safeError(reason, "The folder could not be opened."));
    } finally {
      setOpening(null);
    }
  };
  const selectedGroups = status?.selected_model_groups.map((group) => group.id) ?? [];
  const voiceName = selectedGroups
    .filter((id) => MODEL_GROUP_DETAILS[id]?.kind === "tts")
    .map((id) => MODEL_GROUP_DETAILS[id].label)
    .join(", ") || "Not installed";
  const setupCleaner = status?.selected_model_groups.find((group) => MODEL_GROUP_DETAILS[group.id]?.kind === "cleaner");
  const backend = settings.cleaner_backend_configured
    ? settings.cleaner_backend
    : setupCleaner ? status?.selected_profile : "basic";
  const model = settings.cleaner_backend_configured
    ? settings.cleaner_model
    : setupCleaner ? MODEL_GROUP_DETAILS[setupCleaner.id].model : "";
  const cleanerName = backend === "basic"
    ? "Basic cleanup (no model)"
    : Object.values(MODEL_GROUP_DETAILS).find((group) => group.model === model)?.label
      || model || "No model selected";

  return (
    <div className={active ? "settings-page" : "settings-content"}>
      {active ? (
        <section className="panel conversion-section" aria-label="Voice & models">
          <div className="conversion-section-header conversion-section-header-row">
            <div>
              <h2 className="conversion-group-heading">Voice &amp; models</h2>
              <p className="conversion-section-hint">
                {status?.profiles.find((profile) => profile.id === status.selected_profile)?.label ?? "Runtime not configured"}
                {" · "}{status?.ready ? "Ready" : "Needs setup"}
              </p>
            </div>
            <button className="btn btn-accent-soft" type="button" onClick={onManageModels}>
              Manage models
            </button>
          </div>
          <div className="settings-storage-row">
            <span className="field-label">Voice model</span>
            <span className="settings-path">{voiceName}</span>
          </div>
          <div className="settings-storage-row">
            <span className="field-label">Cleaner</span>
            <span className="settings-path">{cleanerName}</span>
          </div>
        </section>
      ) : null}
      {children}
      {active ? (
        <>
          <section className="panel conversion-section" aria-label="Storage">
            <h2 className="conversion-group-heading">Storage</h2>
            {(["output", "app_data"] as const).map((folder) => (
              <div className="settings-storage-row" key={folder}>
                <div>
                  <span className="field-label">{folder === "output" ? "Output folder" : "App data folder"}</span>
                  <p className="settings-path" title={folders?.[`${folder}_path`]}>
                    {folders?.[`${folder}_path`] ?? "Unavailable"}
                  </p>
                </div>
                <button
                  className="btn btn-ghost"
                  type="button"
                  aria-label={`Open ${folder === "output" ? "output" : "app data"} folder`}
                  disabled={!folders || opening !== null}
                  onClick={() => void openFolder(folder)}
                >
                  {opening === folder ? "Opening…" : "Open"}
                </button>
              </div>
            ))}
            {error ? <p className="setup-model-feedback is-error" role="alert">{error}</p> : null}
          </section>
          <section className="panel conversion-section" aria-label="About">
            <h2 className="conversion-group-heading">About</h2>
            <p>Orpheus {version}</p>
          </section>
        </>
      ) : null}
    </div>
  );
}

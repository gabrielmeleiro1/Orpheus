import { useEffect, useState, type ReactNode } from "react";
import type { ApiClient } from "../../api/client";
import { createSystemApi } from "../../api/resources/system";
import type { RuntimeModelInventory, RuntimeSetupStatusResponse } from "../../types/api";
import { version } from "../../../package.json";
import { MODEL_GROUP_DETAILS } from "./setupModel";
import { safeError } from "./setupPageHelpers";
import "./settings.css";

export function SettingsPage({ active, apiClient, status, onManageModels, children }: {
  active: boolean;
  apiClient: ApiClient | null;
  status: RuntimeSetupStatusResponse | null;
  onManageModels: () => void;
  children: ReactNode;
}) {
  const [folders, setFolders] = useState<{ output_path: string; app_data_path: string } | null>(null);
  const [inventory, setInventory] = useState<RuntimeModelInventory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState<string | null>(null);
  useEffect(() => {
    if (!active || !apiClient) return;
    let current = true;
    setFolders(null);
    setInventory(null);
    setError(null);
    const system = createSystemApi(apiClient.requestJson);
    void Promise.resolve().then(() => system.getStorageFolders({ timeoutMs: 8_000 })).then((next) => {
      if (current) setFolders(next);
    }).catch(() => { if (current) setError("Storage paths could not be loaded. Reopen Settings to retry."); });
    void Promise.resolve().then(() => apiClient.getRuntimeModelInventory({ timeoutMs: 8_000 })).then((next) => {
      if (current) setInventory(next);
    }).catch(() => { if (current) setError("Model storage could not be checked. Use Manage models to retry."); });
    return () => { current = false; };
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
  const installed = inventory?.models.filter((model) => model.downloaded).map((model) => model.id)
    ?? status?.selected_model_groups.filter((group) => group.assets_ready).map((group) => group.id) ?? [];
  const names = (kind: "tts" | "cleaner") => installed.filter((id) => MODEL_GROUP_DETAILS[id]?.kind === kind)
    .map((id) => MODEL_GROUP_DETAILS[id].label).join(", ") || "Not installed";

  return <div className={active ? "settings-page" : "settings-content"}>
    {active ? <>
      <div className="panel-header"><h1>Settings</h1></div>
      <section className="panel conversion-section" aria-label="Voice & models">
        <div className="conversion-section-header conversion-section-header-row">
          <div><h2 className="conversion-group-heading">Voice &amp; models</h2><p className="conversion-section-hint">{status?.profiles.find((profile) => profile.id === status.selected_profile)?.label ?? "Runtime not configured"} · {status?.ready ? "Ready" : "Needs setup"}</p></div>
          <button className="btn btn-accent-soft" onClick={onManageModels}>Manage models</button>
        </div>
        <p>Voice model: {names("tts")}</p><p>Cleaner: {names("cleaner")}</p>
      </section>
      <h2 className="conversion-group-heading">Text cleaning</h2>
    </> : null}
    {children}
    {active ? <>
      <section className="panel conversion-section" aria-label="Storage">
        <h2 className="conversion-group-heading">Storage</h2>
        {(["output", "app_data"] as const).map((folder) => <div className="settings-storage-row" key={folder}>
          <div><span className="field-label">{folder === "output" ? "Output folder" : "App data folder"}</span><p className="settings-path">{folders?.[`${folder}_path`] ?? "Unavailable"}</p></div>
          <button className="btn btn-ghost" aria-label={`Open ${folder === "output" ? "output" : "app data"} folder`} disabled={!folders || opening !== null} onClick={() => void openFolder(folder)}>{opening === folder ? "Opening…" : "Open"}</button>
        </div>)}
        {error ? <p className="setup-model-feedback is-error" role="alert">{error}</p> : null}
      </section>
      <section className="panel conversion-section" aria-label="About"><h2 className="conversion-group-heading">About</h2><p>Orpheus {version}</p></section>
    </> : null}
  </div>;
}

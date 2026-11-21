import { useEffect, useRef, useState, type ReactNode } from "react";
import type { ApiClient } from "../../api/client";
import { version } from "../../../package.json";
import { PathText } from "./PathText";
import { safeError } from "./setupPageHelpers";
import "./settings.css";

type StorageFolder = "output" | "app_data";

const FOLDER_LABELS: Record<StorageFolder, { label: string; open: string }> = {
  output: { label: "Output folder", open: "Open output folder" },
  app_data: { label: "App data folder", open: "Open app data folder" },
};

export function SettingsPage({
  active,
  apiClient,
  models,
  maintenance,
  children,
}: {
  active: boolean;
  apiClient: ApiClient | null;
  /** Model management, rendered in place so downloads never leave Settings. */
  models?: ReactNode;
  maintenance?: ReactNode;
  children: ReactNode;
}) {
  const [folders, setFolders] = useState<{ output_path: string; app_data_path: string; log_path?: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [opening, setOpening] = useState<StorageFolder | null>(null);
  const pageGeneration = useRef(0);
  const pageContext = useRef({ active, apiClient });
  pageContext.current = { active, apiClient };
  useEffect(() => {
    pageGeneration.current += 1;
    setOpening(null);
    if (!active || !apiClient) return;
    let current = true;
    setFolders(null);
    setError(null);
    void Promise.resolve()
      .then(() => apiClient.getStorageFolders({ timeoutMs: 8_000 }))
      .then((next) => {
        if (current) setFolders(next);
      })
      .catch(() => {
        if (current) setError("Storage paths could not be loaded. Reopen Settings to retry.");
      });
    return () => {
      current = false;
      pageGeneration.current += 1;
    };
  }, [active, apiClient]);

  const openFolder = async (folder: StorageFolder) => {
    if (!active || !apiClient || opening) return;
    const generation = pageGeneration.current;
    const isCurrent = () => pageGeneration.current === generation
      && pageContext.current.active && pageContext.current.apiClient === apiClient;
    setOpening(folder);
    setError(null);
    try {
      await apiClient.openStorageFolder(folder, { timeoutMs: 8_000 });
    } catch (reason) {
      if (isCurrent()) setError(safeError(reason, "The folder could not be opened."));
    } finally {
      if (isCurrent()) setOpening(null);
    }
  };
  // The backend reports the log only when it exists; never guess a path that is not there.
  const logPath = folders?.log_path ?? null;

  return (
    <div className={active ? "settings-page page-container" : "settings-content"}>
      {models}
      {children}
      {active ? (
        <>
          {maintenance ? (
            <section className="card settings-section" aria-label="Library maintenance">
              <div className="card-header">
                <h2 className="card-title">Library</h2>
                <p className="settings-section-hint">Checks, backups and cleanup.</p>
              </div>
              {maintenance}
            </section>
          ) : null}
          <section className="card settings-section" aria-label="Storage">
            <div className="card-header">
              <h2 className="card-title">Storage</h2>
              <p className="settings-section-hint">Where Orpheus keeps files.</p>
            </div>
            <div className="settings-rows">
              {(["output", "app_data"] as const).map((folder) => {
                const path = folders?.[`${folder}_path`];
                return (
                  <div className="settings-row settings-storage-row" key={folder}>
                    <span className="field-label">{FOLDER_LABELS[folder].label}</span>
                    {path ? <PathText className="settings-path" path={path} /> : (
                      <span className="settings-path is-empty">{folders || error || !apiClient ? "Unavailable" : "Loading…"}</span>
                    )}
                    <button
                      className="btn btn-secondary btn-sm"
                      type="button"
                      aria-label={FOLDER_LABELS[folder].open}
                      disabled={!folders || opening !== null}
                      onClick={() => void openFolder(folder)}
                    >
                      {opening === folder ? "Opening…" : "Open"}
                    </button>
                  </div>
                );
              })}
              {logPath ? (
                <div className="settings-row settings-storage-row">
                  <span className="field-label">Logs</span>
                  <PathText className="settings-path" path={logPath} />
                </div>
              ) : null}
            </div>
            {error ? <p className="settings-feedback is-error" role="alert">{error}</p> : null}
          </section>
          <section className="card settings-section" aria-label="About">
            <div className="card-header">
              <h2 className="card-title">About</h2>
            </div>
            <dl className="settings-rows">
              <div className="settings-row">
                <dt className="field-label">Version</dt>
                <dd className="tabular-nums">Orpheus {version}</dd>
              </div>
            </dl>
          </section>
        </>
      ) : null}
    </div>
  );
}

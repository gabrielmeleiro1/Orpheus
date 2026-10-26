import type { RequestJson, ApiRequestOptions } from "../http";
import type {
  MaintenanceBackupResponse,
  MaintenanceBackupsResponse,
  MaintenanceCleanupResponse,
  MaintenanceDiagnosticsResponse,
  MaintenanceRestoreResponse,
} from "../../types/api";

export interface CleanupStorageOptions extends ApiRequestOptions {
  removeUnownedFolders?: string[];
}

export function createMaintenanceApi(requestJson: RequestJson) {
  async function getMaintenanceDiagnostics(
    options?: ApiRequestOptions,
  ): Promise<MaintenanceDiagnosticsResponse> {
    return requestJson<MaintenanceDiagnosticsResponse>("/api/maintenance/diagnostics", options);
  }

  async function backupDatabase(options?: ApiRequestOptions): Promise<MaintenanceBackupResponse> {
    return requestJson<MaintenanceBackupResponse>("/api/maintenance/backup", {
      method: "POST",
      ...options,
    });
  }

  async function getDatabaseBackups(options?: ApiRequestOptions): Promise<MaintenanceBackupsResponse> {
    return requestJson<MaintenanceBackupsResponse>("/api/maintenance/backups", options);
  }

  async function restoreDatabaseBackup(
    backupId: string,
    options?: ApiRequestOptions,
  ): Promise<MaintenanceRestoreResponse> {
    return requestJson<MaintenanceRestoreResponse>(
      `/api/maintenance/backups/${encodeURIComponent(backupId)}/restore`,
      { method: "POST", ...options },
    );
  }

  // Folders no book owns are removed only when named here; a plain cleanup keeps them.
  async function cleanupStorage(
    { removeUnownedFolders, ...options }: CleanupStorageOptions = {},
  ): Promise<MaintenanceCleanupResponse> {
    return requestJson<MaintenanceCleanupResponse>("/api/maintenance/cleanup", {
      method: "POST",
      body: JSON.stringify({
        confirm: "cleanup",
        ...(removeUnownedFolders?.length ? { remove_unowned_folders: removeUnownedFolders } : {}),
      }),
      ...options,
    });
  }

  async function openDeletionFolder(path: string, options?: ApiRequestOptions): Promise<{ folder_path: string }> {
    return requestJson<{ folder_path: string }>("/api/maintenance/delete-staging/open-folder", {
      method: "POST",
      body: JSON.stringify({ path }),
      ...options,
    });
  }

  return {
    getMaintenanceDiagnostics,
    backupDatabase,
    getDatabaseBackups,
    restoreDatabaseBackup,
    cleanupStorage,
    openDeletionFolder,
  };
}

import type { RequestJson, ApiRequestOptions } from "../http";
import type {
  MaintenanceBackupResponse,
  MaintenanceCleanupResponse,
  MaintenanceDiagnosticsResponse,
} from "../../types/api";

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

  async function cleanupStorage(options?: ApiRequestOptions): Promise<MaintenanceCleanupResponse> {
    return requestJson<MaintenanceCleanupResponse>("/api/maintenance/cleanup", {
      method: "POST",
      body: JSON.stringify({ confirm: "cleanup" }),
      ...options,
    });
  }

  return {
    openDeletionFolder: (path: string) => requestJson<{ folder_path: string }>("/api/maintenance/delete-staging/open-folder", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
    getMaintenanceDiagnostics,
    backupDatabase,
    cleanupStorage,
  };
}

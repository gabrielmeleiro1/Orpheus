import { invoke } from "@tauri-apps/api/core";
import { useCallback, useMemo, useState } from "react";

import type { ApiClient } from "../../api/client";
import { isAbortLikeError, useLatestRequest } from "../conversion/useLatestRequest";
import { isTauriRuntime } from "../../platform/tauriRuntime";
import type { MaintenanceDiagnosticsResponse } from "../../types/api";
import { createLibraryRepository } from "./libraryRepository";

interface UseLibraryMaintenanceArgs {
  apiClient: ApiClient | null;
  // Invoked after a successful storage cleanup so the list can reflect removed books.
  onAfterCleanup: () => Promise<void> | void;
}

export interface UseLibraryMaintenanceResult {
  maintenanceReport: MaintenanceDiagnosticsResponse | null;
  maintenanceLoading: boolean;
  maintenanceMessage: string;
  canOpenAppData: boolean;
  loadMaintenanceDiagnostics: () => Promise<void>;
  backupDatabase: () => Promise<void>;
  cleanupStorage: () => Promise<void>;
  openAppDataFolder: () => Promise<void>;
}

export function useLibraryMaintenance({
  apiClient,
  onAfterCleanup,
}: UseLibraryMaintenanceArgs): UseLibraryMaintenanceResult {
  const repository = useMemo(() => (apiClient ? createLibraryRepository(apiClient) : null), [apiClient]);

  const [maintenanceReport, setMaintenanceReport] = useState<MaintenanceDiagnosticsResponse | null>(null);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [maintenanceMessage, setMaintenanceMessage] = useState("maintenance idle");

  const beginMaintenanceRequest = useLatestRequest();

  // Shared diagnostics fetch; reuses the caller's staleness guard so it can be
  // chained after backup/cleanup without invalidating the outer request.
  const runDiagnostics = useCallback(
    async (isCurrent: () => boolean): Promise<void> => {
      if (!repository) {
        return;
      }
      const report = await repository.getMaintenanceDiagnostics();
      if (!isCurrent()) {
        return;
      }
      setMaintenanceReport(report);
      setMaintenanceMessage("diagnostics loaded");
    },
    [repository],
  );

  const loadMaintenanceDiagnostics = useCallback(async (): Promise<void> => {
    if (!repository) {
      setMaintenanceMessage("backend disconnected");
      return;
    }

    const isCurrent = beginMaintenanceRequest();
    setMaintenanceLoading(true);
    try {
      await runDiagnostics(isCurrent);
    } catch (error: unknown) {
      if (isCurrent() && !isAbortLikeError(error)) {
        setMaintenanceMessage(error instanceof Error ? error.message : "failed to load diagnostics");
      }
    } finally {
      if (isCurrent()) {
        setMaintenanceLoading(false);
      }
    }
  }, [beginMaintenanceRequest, repository, runDiagnostics]);

  const backupDatabase = useCallback(async (): Promise<void> => {
    if (!repository) {
      setMaintenanceMessage("backend disconnected");
      return;
    }

    const isCurrent = beginMaintenanceRequest();
    setMaintenanceLoading(true);
    try {
      const response = await repository.backupDatabase();
      if (!isCurrent()) {
        return;
      }
      setMaintenanceMessage(`backup created: ${response.backup_path}`);
      await runDiagnostics(isCurrent);
    } catch (error: unknown) {
      if (isCurrent() && !isAbortLikeError(error)) {
        setMaintenanceMessage(error instanceof Error ? error.message : "backup failed");
      }
    } finally {
      if (isCurrent()) {
        setMaintenanceLoading(false);
      }
    }
  }, [beginMaintenanceRequest, repository, runDiagnostics]);

  const cleanupStorage = useCallback(async (): Promise<void> => {
    if (!repository) {
      setMaintenanceMessage("backend disconnected");
      return;
    }
    if (
      typeof window.confirm === "function" &&
      !window.confirm("Clean stale storage records and orphan files? A database backup will be created first.")
    ) {
      return;
    }

    const isCurrent = beginMaintenanceRequest();
    setMaintenanceLoading(true);
    try {
      await repository.cleanupStorage();
      if (!isCurrent()) {
        return;
      }
      setMaintenanceMessage("cleanup complete");
      await runDiagnostics(isCurrent);
      await onAfterCleanup();
    } catch (error: unknown) {
      if (isCurrent() && !isAbortLikeError(error)) {
        setMaintenanceMessage(error instanceof Error ? error.message : "cleanup failed");
      }
    } finally {
      if (isCurrent()) {
        setMaintenanceLoading(false);
      }
    }
  }, [beginMaintenanceRequest, onAfterCleanup, repository, runDiagnostics]);

  const openAppDataFolder = useCallback(async (): Promise<void> => {
    if (isTauriRuntime()) {
      await invoke("open_app_data_folder");
    }
  }, []);

  return {
    maintenanceReport,
    maintenanceLoading,
    maintenanceMessage,
    canOpenAppData: isTauriRuntime(),
    loadMaintenanceDiagnostics,
    backupDatabase,
    cleanupStorage,
    openAppDataFolder,
  };
}

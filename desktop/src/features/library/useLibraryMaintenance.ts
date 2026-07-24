import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { isAbortLikeError, useLatestRequest } from "../conversion/useLatestRequest";
import { isTauriRuntime } from "../../platform/tauriRuntime";
import type { MaintenanceDiagnosticsResponse } from "../../types/api";
import { createLibraryRepository } from "./libraryRepository";

interface UseLibraryMaintenanceArgs {
  apiClient: ApiClient | null;
  // Invoked after a successful storage cleanup so the list can reflect removed books.
  onAfterCleanup: () => Promise<void> | void;
  getReloadContextGeneration?: () => number;
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

const staticReloadContextGeneration = () => 0;

export function useLibraryMaintenance({
  apiClient,
  onAfterCleanup,
  getReloadContextGeneration = staticReloadContextGeneration,
}: UseLibraryMaintenanceArgs): UseLibraryMaintenanceResult {
  const repository = useMemo(() => (apiClient ? createLibraryRepository(apiClient) : null), [apiClient]);

  const [maintenanceReport, setMaintenanceReport] = useState<MaintenanceDiagnosticsResponse | null>(null);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [maintenanceMessage, setMaintenanceMessage] = useState("Maintenance idle");

  const maintenanceContextGenerationRef = useRef(0);
  const maintenanceContextIdentityRef = useRef({
    apiClient,
    onAfterCleanup,
    getReloadContextGeneration,
  });
  if (
    maintenanceContextIdentityRef.current.apiClient !== apiClient ||
    maintenanceContextIdentityRef.current.onAfterCleanup !== onAfterCleanup ||
    maintenanceContextIdentityRef.current.getReloadContextGeneration !== getReloadContextGeneration
  ) {
    maintenanceContextIdentityRef.current = {
      apiClient,
      onAfterCleanup,
      getReloadContextGeneration,
    };
    maintenanceContextGenerationRef.current += 1;
  }
  const maintenanceContextGeneration = maintenanceContextGenerationRef.current;
  const beginMaintenanceRequest = useLatestRequest();
  const beginOwnedMaintenanceRequest = useCallback(() => {
    const contextGeneration = maintenanceContextGenerationRef.current;
    const isLatest = beginMaintenanceRequest();
    return () =>
      isLatest() &&
      maintenanceContextGenerationRef.current === contextGeneration;
  }, [beginMaintenanceRequest]);

  useEffect(() => {
    setMaintenanceLoading(false);
  }, [maintenanceContextGeneration]);

  // Shared diagnostics fetch; reuses the caller's staleness guard so it can be
  // chained after backup/cleanup without invalidating the outer request.
  const runDiagnostics = useCallback(
    async (isCurrent: () => boolean): Promise<boolean> => {
      if (!repository) {
        return false;
      }
      const report = await repository.getMaintenanceDiagnostics();
      if (!isCurrent()) {
        return false;
      }
      setMaintenanceReport(report);
      setMaintenanceMessage("diagnostics loaded");
      return true;
    },
    [repository],
  );

  const loadMaintenanceDiagnostics = useCallback(async (): Promise<void> => {
    if (!repository) {
      setMaintenanceMessage("backend disconnected");
      return;
    }

    const isCurrent = beginOwnedMaintenanceRequest();
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
  }, [beginOwnedMaintenanceRequest, repository, runDiagnostics]);

  const backupDatabase = useCallback(async (): Promise<void> => {
    if (!repository) {
      setMaintenanceMessage("backend disconnected");
      return;
    }

    const isCurrent = beginOwnedMaintenanceRequest();
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
  }, [beginOwnedMaintenanceRequest, repository, runDiagnostics]);

  const cleanupStorage = useCallback(async (): Promise<void> => {
    if (!repository) {
      setMaintenanceMessage("backend disconnected");
      return;
    }
    const isCurrent = beginOwnedMaintenanceRequest();
    const reloadContextGeneration = getReloadContextGeneration();
    const isCleanupCurrent = () =>
      isCurrent() &&
      getReloadContextGeneration() === reloadContextGeneration;
    setMaintenanceLoading(true);
    try {
      await repository.cleanupStorage();
      if (!isCleanupCurrent()) {
        return;
      }
      setMaintenanceMessage("cleanup complete");
      const diagnosticsLoaded = await runDiagnostics(isCleanupCurrent);
      if (!diagnosticsLoaded || !isCleanupCurrent()) {
        return;
      }
      await onAfterCleanup();
    } catch (error: unknown) {
      if (isCleanupCurrent() && !isAbortLikeError(error)) {
        setMaintenanceMessage(error instanceof Error ? error.message : "cleanup failed");
      }
    } finally {
      if (isCurrent()) {
        setMaintenanceLoading(false);
      }
    }
  }, [beginOwnedMaintenanceRequest, getReloadContextGeneration, onAfterCleanup, repository, runDiagnostics]);

  const openAppDataFolder = useCallback(async (): Promise<void> => {
    if (!isTauriRuntime()) {
      return;
    }
    const isCurrent = beginOwnedMaintenanceRequest();
    try {
      await invoke("open_app_data_folder");
    } catch (error: unknown) {
      if (!isCurrent()) {
        return;
      }
      const detail = error instanceof Error ? error.message : String(error || "unknown error");
      setMaintenanceMessage(`Could not open app data folder: ${detail}`);
    }
  }, [beginOwnedMaintenanceRequest]);

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

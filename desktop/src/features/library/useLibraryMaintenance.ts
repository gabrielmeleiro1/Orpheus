import { backupSavedMessage, cleanupResultMessage, restoreResultMessage } from "./maintenanceModel";
import { invoke } from "@tauri-apps/api/core";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ApiError, type ApiClient } from "../../api/client";
import { createMaintenanceApi } from "../../api/resources/maintenance";
import { useGenerationGuard } from "../../state/generationGuard";
import { useLatestRequest } from "../conversion/useLatestRequest";
import { isTauriRuntime } from "../../platform/tauriRuntime";
import type { MaintenanceBackup, MaintenanceDiagnosticsResponse } from "../../types/api";
import { clearLibraryPlaybackCheckpoints } from "../player/playbackCheckpoint";
import type { LibrarySession } from "./librarySession";
import { createLibraryRepository } from "./libraryRepository";

// Diagnostics scan storage and backup listing inspects database files. Give
// disk reads more time than cached state without holding the panel forever.
const MAINTENANCE_DIAGNOSTICS_TIMEOUT_MS = 30_000;
const MAINTENANCE_BACKUP_LIST_TIMEOUT_MS = 30_000;

interface UseLibraryMaintenanceArgs {
  apiClient: ApiClient | null;
  backendDisconnected?: boolean;
  session?: LibrarySession;
  // Reload the list after storage cleanup or database restore.
  onAfterCleanup: () => Promise<void> | void;
  getReloadContextGeneration: () => number;
}

export interface UseLibraryMaintenanceResult {
  maintenanceReport: MaintenanceDiagnosticsResponse | null;
  maintenanceBackups: MaintenanceBackup[];
  maintenanceLoading: boolean;
  maintenanceMessage: string;
  canOpenAppData: boolean;
  loadMaintenanceDiagnostics: () => Promise<void>;
  backupDatabase: () => Promise<void>;
  restoreBackup: (id: string) => Promise<void>;
  cleanupStorage: (removeUnownedFolders?: string[]) => Promise<void>;
  openAppDataFolder: () => Promise<void>;
}

function libraryGeneration(report: MaintenanceDiagnosticsResponse): string | null {
  if (report.library_id && report.library_epoch) return JSON.stringify([report.library_id, report.library_epoch]);
  return "library_generation" in report && typeof report.library_generation === "string"
    ? report.library_generation : null;
}

export function useLibraryMaintenance({
  apiClient,
  backendDisconnected = false,
  session,
  onAfterCleanup,
  getReloadContextGeneration,
}: UseLibraryMaintenanceArgs): UseLibraryMaintenanceResult {
  const repository = useMemo(() => (apiClient ? createLibraryRepository(apiClient) : null), [apiClient]);
  const maintenanceApi = useMemo(() => (apiClient ? createMaintenanceApi(apiClient.requestJson) : null), [apiClient]);

  const [maintenanceReport, setMaintenanceReport] = useState<MaintenanceDiagnosticsResponse | null>(null);
  const [maintenanceBackups, setMaintenanceBackups] = useState<MaintenanceBackup[]>([]);
  const [maintenanceLoading, setMaintenanceLoading] = useState(false);
  const [maintenanceMessage, setMaintenanceMessage] = useState("Maintenance idle");

  const reloadRef = useRef(onAfterCleanup);
  useLayoutEffect(() => {
    reloadRef.current = onAfterCleanup;
  });
  const backupsRef = useRef<MaintenanceBackup[]>([]);
  const restoreInFlightRef = useRef<symbol | null>(null);
  const uncertainRestoreRef = useRef(false);
  const maintenanceContextGuard = useGenerationGuard();
  const maintenanceContextIdentityRef = useRef({ apiClient, backendDisconnected });
  if (
    maintenanceContextIdentityRef.current.apiClient !== apiClient ||
    maintenanceContextIdentityRef.current.backendDisconnected !== backendDisconnected
  ) {
    maintenanceContextIdentityRef.current = { apiClient, backendDisconnected };
    maintenanceContextGuard.invalidate();
    if (restoreInFlightRef.current) uncertainRestoreRef.current = true;
  }
  const maintenanceContextGeneration = maintenanceContextGuard.current;
  const beginMaintenanceRequest = useLatestRequest();
  const beginOpenFolderRequest = useLatestRequest();
  const beginOwnedMaintenanceRequest = useCallback((beginRequest = beginMaintenanceRequest) => {
    const ownsContext = maintenanceContextGuard.capture();
    const isLatest = beginRequest();
    return () => isLatest() && ownsContext();
  }, [beginMaintenanceRequest]);

  useEffect(() => {
    setMaintenanceLoading(false);
    if (uncertainRestoreRef.current) {
      setMaintenanceMessage("Restore outcome could not be verified. Writes are paused. Reload diagnostics to reconnect.");
    }
  }, [maintenanceContextGeneration]);

  useEffect(() => {
    setMaintenanceReport(null);
    setMaintenanceBackups([]);
  }, [apiClient]);

  // Shared diagnostics fetch; reuses the caller's staleness guard so it can be
  // chained after backup/cleanup without invalidating the outer request.
  const runDiagnostics = useCallback(
    // A chained refresh keeps the result message of the action it follows.
    async (isCurrent: () => boolean, announce = true): Promise<boolean> => {
      if (!repository) {
        return false;
      }
      const report = await repository.getMaintenanceDiagnostics({ timeoutMs: MAINTENANCE_DIAGNOSTICS_TIMEOUT_MS });
      if (!isCurrent()) {
        return false;
      }
      if (uncertainRestoreRef.current) {
        if (libraryGeneration(report) === null) throw new Error("Library generation unavailable. Writes are paused.");
        clearLibraryPlaybackCheckpoints();
        session?.invalidate();
        await reloadRef.current();
        if (!isCurrent()) return false;
        uncertainRestoreRef.current = false;
        session?.resume();
      }
      setMaintenanceReport(report);
      if (maintenanceApi) {
        const backups = await maintenanceApi.getDatabaseBackups({ timeoutMs: MAINTENANCE_BACKUP_LIST_TIMEOUT_MS });
        if (!isCurrent()) {
          return false;
        }
        backupsRef.current = backups.items;
        setMaintenanceBackups(backups.items);
      }
      if (announce) setMaintenanceMessage("Diagnostics loaded");
      return true;
    },
    [maintenanceApi, repository, session],
  );

  const loadMaintenanceDiagnostics = useCallback(async (): Promise<void> => {
    if (restoreInFlightRef.current) return;
    if (!repository) {
      setMaintenanceMessage("Backend disconnected");
      return;
    }

    const isCurrent = beginOwnedMaintenanceRequest();
    setMaintenanceLoading(true);
    try {
      await runDiagnostics(isCurrent);
    } catch (error: unknown) {
      if (isCurrent() && !(error instanceof ApiError && error.code === "aborted")) {
        setMaintenanceMessage(error instanceof Error ? error.message : "failed to load diagnostics");
      }
    } finally {
      if (isCurrent()) {
        setMaintenanceLoading(false);
      }
    }
  }, [beginOwnedMaintenanceRequest, repository, runDiagnostics]);

  const restoreBackup = useCallback(async (id: string): Promise<void> => {
    if (restoreInFlightRef.current) return;
    if (uncertainRestoreRef.current) {
      setMaintenanceMessage("Restore outcome could not be verified. Writes are paused. Reload diagnostics to reconnect.");
      return;
    }
    if (!maintenanceApi) {
      setMaintenanceMessage("Backend disconnected");
      return;
    }
    const token = Symbol();
    restoreInFlightRef.current = token;
    const isCurrent = beginOwnedMaintenanceRequest();
    setMaintenanceLoading(true);
    let beforeGeneration: string | null = null;
    let requestSent = false;
    let invalidated = false;
    let canResume = true;
    try {
      await session?.suspendAndDrain();
      if (!isCurrent()) return;
      try {
        if (repository) beforeGeneration = libraryGeneration(await repository.getMaintenanceDiagnostics({ timeoutMs: MAINTENANCE_DIAGNOSTICS_TIMEOUT_MS }));
      } catch {
        // A successful restore response still establishes the replacement.
      }
      if (!isCurrent()) return;
      requestSent = true;
      const response = await maintenanceApi.restoreDatabaseBackup(id);
      if (!isCurrent()) {
        return;
      }
      clearLibraryPlaybackCheckpoints();
      session?.invalidate();
      invalidated = true;
      await reloadRef.current();
      if (!isCurrent()) {
        return;
      }
      await runDiagnostics(isCurrent, false);
      if (isCurrent()) {
        setMaintenanceMessage(restoreResultMessage(response));
      }
    } catch (error: unknown) {
      const rejected = error instanceof ApiError && error.code === "http" &&
        error.status !== null && error.status >= 400 && error.status < 500;
      if (isCurrent() && requestSent && !invalidated && !rejected) {
        canResume = false;
        try {
          const report = await repository!.getMaintenanceDiagnostics({ timeoutMs: MAINTENANCE_DIAGNOSTICS_TIMEOUT_MS });
          if (!isCurrent()) return;
          const afterGeneration = libraryGeneration(report);
          if (afterGeneration === null) throw new Error("Library generation unavailable");
          if (beforeGeneration === null || afterGeneration !== beforeGeneration) {
            clearLibraryPlaybackCheckpoints();
            session?.invalidate();
            await reloadRef.current();
          }
          if (!isCurrent()) return;
          setMaintenanceReport(report);
          canResume = true;
        } catch {
          uncertainRestoreRef.current = true;
          clearLibraryPlaybackCheckpoints();
          session?.invalidate();
          if (isCurrent()) {
            setMaintenanceMessage("Restore outcome could not be verified. Writes are paused. Reload diagnostics to reconnect.");
          }
          return;
        }
      }
      if (isCurrent() && !(error instanceof ApiError && error.code === "aborted")) {
        setMaintenanceMessage(error instanceof Error ? error.message : "restore failed");
      }
    } finally {
      if (!isCurrent() && requestSent) {
        uncertainRestoreRef.current = true;
        clearLibraryPlaybackCheckpoints();
        session?.invalidate();
        canResume = false;
      }
      if (canResume && !uncertainRestoreRef.current) session?.resume();
      if (restoreInFlightRef.current === token) restoreInFlightRef.current = null;
      if (isCurrent()) {
        setMaintenanceLoading(false);
      }
    }
  }, [beginOwnedMaintenanceRequest, maintenanceApi, repository, runDiagnostics, session]);

  const backupDatabase = useCallback(async (): Promise<void> => {
    if (restoreInFlightRef.current) return;
    if (!repository) {
      setMaintenanceMessage("Backend disconnected");
      return;
    }

    const isCurrent = beginOwnedMaintenanceRequest();
    setMaintenanceLoading(true);
    try {
      const response = await repository.backupDatabase();
      if (!isCurrent()) {
        return;
      }
      setMaintenanceMessage(backupSavedMessage(response.backup_path));
      // The refreshed list carries the new backup's label.
      if (await runDiagnostics(isCurrent, false)) {
        setMaintenanceMessage(backupSavedMessage(response.backup_path, backupsRef.current));
      }
    } catch (error: unknown) {
      if (isCurrent() && !(error instanceof ApiError && error.code === "aborted")) {
        setMaintenanceMessage(error instanceof Error ? error.message : "backup failed");
      }
    } finally {
      if (isCurrent()) {
        setMaintenanceLoading(false);
      }
    }
  }, [beginOwnedMaintenanceRequest, repository, runDiagnostics]);

  const cleanupStorage = useCallback(async (removeUnownedFolders?: string[]): Promise<void> => {
    if (restoreInFlightRef.current) return;
    if (!repository) {
      setMaintenanceMessage("Backend disconnected");
      return;
    }
    const isCurrent = beginOwnedMaintenanceRequest();
    const reload = onAfterCleanup;
    const reloadContextGeneration = getReloadContextGeneration();
    const isCleanupCurrent = () =>
      isCurrent() &&
      reloadRef.current === reload &&
      getReloadContextGeneration() === reloadContextGeneration;
    setMaintenanceLoading(true);
    try {
      const cleanup = () => repository.cleanupStorage(removeUnownedFolders?.length ? { removeUnownedFolders } : undefined);
      const result = session ? await session.runMutation(session.generation, cleanup) : await cleanup();
      if (!isCleanupCurrent()) {
        return;
      }
      setMaintenanceMessage(cleanupResultMessage(result));
      const diagnosticsLoaded = await runDiagnostics(isCleanupCurrent, false);
      if (!diagnosticsLoaded || !isCleanupCurrent()) {
        return;
      }
      await onAfterCleanup();
    } catch (error: unknown) {
      if (isCleanupCurrent() && !(error instanceof ApiError && error.code === "aborted")) {
        setMaintenanceMessage(error instanceof Error ? error.message : "cleanup failed");
      }
    } finally {
      if (isCurrent()) {
        setMaintenanceLoading(false);
      }
    }
  }, [beginOwnedMaintenanceRequest, getReloadContextGeneration, onAfterCleanup, repository, runDiagnostics, session]);

  const openAppDataFolder = useCallback(async (): Promise<void> => {
    if (restoreInFlightRef.current) return;
    if (!isTauriRuntime()) {
      return;
    }
    // Opening a folder does not supersede an active diagnostics or cleanup
    // operation, which still owns its loading state and follow-up work.
    const isCurrent = beginOwnedMaintenanceRequest(beginOpenFolderRequest);
    try {
      await invoke("open_app_data_folder");
    } catch (error: unknown) {
      if (!isCurrent()) {
        return;
      }
      const detail = error instanceof Error ? error.message : String(error || "unknown error");
      setMaintenanceMessage(`Could not open app data folder: ${detail}`);
    }
  }, [beginOpenFolderRequest, beginOwnedMaintenanceRequest]);

  return {
    maintenanceReport,
    maintenanceBackups,
    maintenanceLoading,
    maintenanceMessage,
    canOpenAppData: isTauriRuntime(),
    loadMaintenanceDiagnostics,
    backupDatabase,
    restoreBackup,
    cleanupStorage,
    openAppDataFolder,
  };
}

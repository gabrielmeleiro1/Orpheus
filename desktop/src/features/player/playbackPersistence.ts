import { useCallback, useEffect, useRef, type RefObject } from "react";

import type { ApiClient } from "../../api/client";
import { PLAYBACK_SAVE_INTERVAL_SEC } from "../dashboard/dashboardHelpers";
import type { UseLibraryResult } from "../library/useLibrary";

// Consecutive silent save failures tolerated before telling the user that
// resume is broken. Automatic saves fire about once a second, so this rides
// out a brief blip without sitting quiet through a real outage.
const PLAYBACK_SAVE_FAILURE_NOTICE_THRESHOLD = 5;

export interface PersistPlaybackOptions {
  bookId?: number | null;
  positionSec?: number;
  force?: boolean;
  silent?: boolean;
}

interface UsePlaybackPersistenceArgs {
  apiClient: ApiClient | null;
  library: UseLibraryResult;
  audioRef: RefObject<HTMLAudioElement | null>;
  loadedBookId: number | null;
  onSaveFailed: () => void;
}

export interface PlaybackPersistence {
  persistBookPlaybackPosition: (
    bookId: number,
    positionSec: number,
    options?: PersistPlaybackOptions,
  ) => Promise<number | null>;
  persistLoadedBookPlaybackPosition: (options?: PersistPlaybackOptions) => Promise<number | null>;
  restorePendingResumePosition: (audio: HTMLAudioElement | null, expectedBookId?: number | null) => void;
  waitForAudioReady: (audio: HTMLAudioElement | null) => Promise<void>;
  setPendingResume: (bookId: number, positionSec: number) => void;
  clearPendingResume: () => void;
  markPlaybackSaved: (bookId: number, positionSec: number) => void;
}

/**
 * Owns resume-position persistence: throttled saves, pending-resume seeking,
 * metadata readiness waiting, and the before-unload flush.
 */
export function usePlaybackPersistence({
  apiClient,
  library,
  audioRef,
  loadedBookId,
  onSaveFailed,
}: UsePlaybackPersistenceArgs): PlaybackPersistence {
  const pendingResumeBookIdRef = useRef<number | null>(null);
  const pendingResumePositionSecRef = useRef(0);
  const lastSavedPlaybackBookIdRef = useRef<number | null>(null);
  const lastSavedPlaybackSecRef = useRef<number | null>(null);
  const consecutiveSaveFailuresRef = useRef(0);

  const setPendingResume = useCallback((bookId: number, positionSec: number) => {
    pendingResumeBookIdRef.current = bookId;
    pendingResumePositionSecRef.current = positionSec;
  }, []);

  const clearPendingResume = useCallback(() => {
    pendingResumeBookIdRef.current = null;
    pendingResumePositionSecRef.current = 0;
  }, []);

  const markPlaybackSaved = useCallback((bookId: number, positionSec: number) => {
    lastSavedPlaybackBookIdRef.current = bookId;
    lastSavedPlaybackSecRef.current = positionSec;
  }, []);

  const persistBookPlaybackPosition = useCallback(
    async (bookId: number, positionSec: number, options: PersistPlaybackOptions = {}): Promise<number | null> => {
      if (!apiClient || !bookId) {
        return null;
      }

      const normalizedPosition = Math.max(0, Math.round((Number(positionSec) || 0) * 10) / 10);
      const force = Boolean(options.force);

      if (
        !force &&
        lastSavedPlaybackBookIdRef.current === bookId &&
        lastSavedPlaybackSecRef.current !== null &&
        Math.abs(normalizedPosition - lastSavedPlaybackSecRef.current) < PLAYBACK_SAVE_INTERVAL_SEC
      ) {
        return lastSavedPlaybackSecRef.current;
      }

      const previousSavedBookId = lastSavedPlaybackBookIdRef.current;
      const previousSavedSec = lastSavedPlaybackSecRef.current;
      lastSavedPlaybackBookIdRef.current = bookId;
      lastSavedPlaybackSecRef.current = normalizedPosition;

      try {
        const response = await apiClient.updateBookPlaybackPosition(bookId, normalizedPosition);
        const savedPosition = Number(response.position_sec);
        const resolved = Number.isFinite(savedPosition) ? savedPosition : normalizedPosition;
        lastSavedPlaybackSecRef.current = resolved;
        consecutiveSaveFailuresRef.current = 0;
        if (library.selectedBookId === bookId) {
          library.setSelectedPlaybackPosition(resolved);
        }
        return resolved;
      } catch {
        // The throttle marker was set optimistically above. Roll it back, or
        // the next per-second save is skipped as "already saved" and the
        // position silently stops advancing for the rest of the session.
        lastSavedPlaybackBookIdRef.current = previousSavedBookId;
        lastSavedPlaybackSecRef.current = previousSavedSec;
        consecutiveSaveFailuresRef.current += 1;
        // Automatic saves stay quiet about a blip, but a sustained failure
        // means resume is broken and the user should hear about it.
        if (
          !options.silent ||
          consecutiveSaveFailuresRef.current >= PLAYBACK_SAVE_FAILURE_NOTICE_THRESHOLD
        ) {
          onSaveFailed();
        }
        return null;
      }
    },
    [apiClient, library, onSaveFailed],
  );

  const persistLoadedBookPlaybackPosition = useCallback(
    async (options: PersistPlaybackOptions = {}): Promise<number | null> => {
      const bookId = options.bookId ?? loadedBookId;
      if (!bookId) {
        return null;
      }

      const audio = audioRef.current;
      const fallbackPosition =
        library.selectedBookId === bookId
          ? Number(library.selectedBookDetail?.playback_position_sec || 0)
          : 0;

      const resolvedPosition =
        typeof options.positionSec === "number"
          ? options.positionSec
          : audio && loadedBookId === bookId && audio.src
            ? Number(audio.currentTime || 0)
            : fallbackPosition;

      return persistBookPlaybackPosition(bookId, resolvedPosition, options);
    },
    [
      audioRef,
      library.selectedBookDetail?.playback_position_sec,
      library.selectedBookId,
      loadedBookId,
      persistBookPlaybackPosition,
    ],
  );

  const waitForAudioReady = useCallback((audio: HTMLAudioElement | null): Promise<void> => {
    if (!audio || audio.readyState >= 1) {
      return Promise.resolve();
    }

    return new Promise((resolve) => {
      let settled = false;
      let timeoutId: number | null = null;

      const finish = () => {
        if (settled) {
          return;
        }
        settled = true;
        audio.removeEventListener("loadedmetadata", finish);
        audio.removeEventListener("durationchange", finish);
        audio.removeEventListener("canplay", finish);
        audio.removeEventListener("error", finish);
        if (timeoutId !== null) {
          window.clearTimeout(timeoutId);
        }
        resolve();
      };

      audio.addEventListener("loadedmetadata", finish, { once: true });
      audio.addEventListener("durationchange", finish, { once: true });
      audio.addEventListener("canplay", finish, { once: true });
      audio.addEventListener("error", finish, { once: true });
      timeoutId = window.setTimeout(finish, 2000);
    });
  }, []);

  const restorePendingResumePosition = useCallback(
    (audio: HTMLAudioElement | null, expectedBookId?: number | null) => {
      const resolvedBookId = expectedBookId ?? loadedBookId;
      if (!audio || pendingResumeBookIdRef.current !== resolvedBookId) {
        return;
      }

      const target = Math.max(0, Number(pendingResumePositionSecRef.current || 0));
      const duration = Number.isFinite(audio.duration) ? audio.duration : target;
      const safeTarget = Math.max(0, Math.min(duration || target, target));

      if (safeTarget > 0) {
        try {
          audio.currentTime = safeTarget;
        } catch {
          return;
        }
      }

      pendingResumeBookIdRef.current = null;
      pendingResumePositionSecRef.current = 0;
    },
    [loadedBookId],
  );

  useEffect(() => {
    const onBeforeUnload = () => {
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    };

    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, [persistLoadedBookPlaybackPosition]);

  return {
    persistBookPlaybackPosition,
    persistLoadedBookPlaybackPosition,
    restorePendingResumePosition,
    waitForAudioReady,
    setPendingResume,
    clearPendingResume,
    markPlaybackSaved,
  };
}

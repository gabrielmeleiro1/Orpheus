import { useCallback, useEffect, useRef, type RefObject } from "react";

import type { ApiClient } from "../../api/client";
import { PLAYBACK_SAVE_INTERVAL_SEC } from "../dashboard/dashboardHelpers";
import type { UseLibraryResult } from "../library/useLibrary";
import {
  playbackCheckpointBookIdentity,
  readPlaybackCheckpoint,
  writePlaybackCheckpoint,
} from "./playbackCheckpoint";

// Consecutive silent save failures tolerated before telling the user that
// resume is broken. Automatic saves fire about once a second, so this rides
// out a brief blip without sitting quiet through a real outage.
const PLAYBACK_SAVE_FAILURE_NOTICE_THRESHOLD = 5;

export interface PersistPlaybackOptions {
  bookId?: number | null;
  bookIdentity?: string;
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
  setPendingResume: (bookId: number, positionSec: number, bookIdentity: string) => void;
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
  const lastSavedPlaybackRef = useRef<Map<number, { positionSec: number; token: number }>>(new Map());
  const committedPlaybackRef = useRef<Map<number, number>>(new Map());
  const saveSequenceRef = useRef(0);
  const saveTailsRef = useRef<Map<number, Promise<number | null>>>(new Map());
  const consecutiveSaveFailuresRef = useRef<Map<number, number>>(new Map());
  const bookIdentityByIdRef = useRef<Map<number, string>>(new Map());
  const playbackClientIdentityRef = useRef(apiClient);
  if (playbackClientIdentityRef.current !== apiClient) {
    playbackClientIdentityRef.current = apiClient;
    lastSavedPlaybackRef.current.clear();
    committedPlaybackRef.current.clear();
    consecutiveSaveFailuresRef.current.clear();
    saveTailsRef.current.clear();
    bookIdentityByIdRef.current.clear();
  }
  const selectedBookIdRef = useRef(library.selectedBookId);
  selectedBookIdRef.current = library.selectedBookId;
  const loadedBookIdRef = useRef(loadedBookId);
  loadedBookIdRef.current = loadedBookId;

  const setPendingResume = useCallback((bookId: number, positionSec: number, bookIdentity: string) => {
    bookIdentityByIdRef.current.set(bookId, bookIdentity);
    pendingResumeBookIdRef.current = bookId;
    pendingResumePositionSecRef.current =
      readPlaybackCheckpoint(bookId, bookIdentity) ?? positionSec;
  }, []);

  const clearPendingResume = useCallback(() => {
    pendingResumeBookIdRef.current = null;
    pendingResumePositionSecRef.current = 0;
  }, []);

  const markPlaybackSaved = useCallback((bookId: number, positionSec: number) => {
    const bookIdentity = bookIdentityByIdRef.current.get(bookId);
    if (bookIdentity) {
      writePlaybackCheckpoint(bookId, positionSec, bookIdentity);
    }
    lastSavedPlaybackRef.current.set(bookId, {
      positionSec,
      token: ++saveSequenceRef.current,
    });
  }, []);

  const persistBookPlaybackPosition = useCallback(
    async (bookId: number, positionSec: number, options: PersistPlaybackOptions = {}): Promise<number | null> => {
      const requestApiClient = apiClient;
      if (playbackClientIdentityRef.current !== requestApiClient) {
        return null;
      }

      let bookIdentity = options.bookIdentity ?? bookIdentityByIdRef.current.get(bookId);
      if (!bookIdentity && library.selectedBookDetail?.id === bookId) {
        bookIdentity = playbackCheckpointBookIdentity(library.selectedBookDetail);
        bookIdentityByIdRef.current.set(bookId, bookIdentity);
      }
      const checkpointPosition = Math.max(0, Number(positionSec) || 0);
      if (bookIdentity) {
        writePlaybackCheckpoint(bookId, checkpointPosition, bookIdentity);
      }
      if (!requestApiClient || !bookId) {
        return null;
      }

      // Client identity is the whole fence: the ref only ever changes together
      // with the client it tracks, so a live save owns its client exactly while
      // the mounted hook still points at it.
      const ownsCurrentClient = () => playbackClientIdentityRef.current === requestApiClient;
      const normalizedPosition = Math.round(checkpointPosition * 10) / 10;
      const force = Boolean(options.force);
      const previousMarker = lastSavedPlaybackRef.current.get(bookId) ?? null;

      if (
        !force &&
        previousMarker &&
        Math.abs(normalizedPosition - previousMarker.positionSec) < PLAYBACK_SAVE_INTERVAL_SEC
      ) {
        return previousMarker.positionSec;
      }

      const token = ++saveSequenceRef.current;
      lastSavedPlaybackRef.current.set(bookId, { positionSec: normalizedPosition, token });
      const previousTail = saveTailsRef.current.get(bookId) ?? Promise.resolve(null);
      const operation = previousTail.catch(() => null).then(async () => {
        if (!ownsCurrentClient()) {
          return null;
        }
        try {
          const response = await requestApiClient.updateBookPlaybackPosition(bookId, normalizedPosition);
          if (!ownsCurrentClient()) {
            return null;
          }
          const savedPosition = Number(response.position_sec);
          const resolved = Number.isFinite(savedPosition) ? savedPosition : normalizedPosition;
          committedPlaybackRef.current.set(bookId, resolved);
          const currentMarker = lastSavedPlaybackRef.current.get(bookId);
          if (currentMarker?.token === token) {
            lastSavedPlaybackRef.current.set(bookId, { positionSec: resolved, token });
            if (selectedBookIdRef.current === bookId) {
              library.setSelectedPlaybackPosition(resolved);
            }
          }
          consecutiveSaveFailuresRef.current.delete(bookId);
          return resolved;
        } catch {
          if (!ownsCurrentClient()) {
            return null;
          }
          const currentMarker = lastSavedPlaybackRef.current.get(bookId);
          if (currentMarker?.token === token) {
            const committedPosition = committedPlaybackRef.current.get(bookId);
            if (committedPosition !== undefined) {
              lastSavedPlaybackRef.current.set(bookId, { positionSec: committedPosition, token });
            } else {
              lastSavedPlaybackRef.current.delete(bookId);
            }
          }
          const failureCount = (consecutiveSaveFailuresRef.current.get(bookId) ?? 0) + 1;
          consecutiveSaveFailuresRef.current.set(bookId, failureCount);
          const ownsCurrentPlayback =
            selectedBookIdRef.current === bookId && loadedBookIdRef.current === bookId;
          if (
            ownsCurrentPlayback &&
            (!options.silent || failureCount >= PLAYBACK_SAVE_FAILURE_NOTICE_THRESHOLD)
          ) {
            onSaveFailed();
          }
          return null;
        }
      });
      let trackedTail!: Promise<number | null>;
      trackedTail = operation.finally(() => {
        if (saveTailsRef.current.get(bookId) === trackedTail) {
          saveTailsRef.current.delete(bookId);
        }
      });
      saveTailsRef.current.set(bookId, trackedTail);
      return trackedTail;
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
      const hasExplicitPosition = typeof options.positionSec === "number";
      const ownsLoadedAudio = Boolean(
        audio &&
        loadedBookId === bookId &&
        audio.src &&
        bookIdentityByIdRef.current.has(bookId)
      );
      if (!hasExplicitPosition && !ownsLoadedAudio) {
        return null;
      }
      const resolvedPosition = hasExplicitPosition
        ? options.positionSec!
        : Number(audio!.currentTime || 0);

      return persistBookPlaybackPosition(bookId, resolvedPosition, options);
    },
    [
      audioRef,
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

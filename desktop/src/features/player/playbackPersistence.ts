import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

import { isLibraryReplacedError, type ApiClient } from "../../api/client";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail } from "../../types/api";
import { chapterPlaybackAnchor } from "./chapterPositionModel";
import {
  playbackCheckpointBookIdentity,
  readPlaybackCheckpoint,
  readPlaybackResumePosition,
  writePlaybackCheckpoint,
} from "./playbackCheckpoint";

// Playback position is saved after this much playback, and on pause, seek and book changes.
const PLAYBACK_SAVE_INTERVAL_SEC = 15;
// Consecutive save failures tolerated before telling the user that resume is
// broken. Automatic retries are a save interval apart, so two failures ride out
// a single blip and still surface a real outage within about half a minute.
const PLAYBACK_SAVE_FAILURE_NOTICE_THRESHOLD = 2;
// A stalled write must release later saves and the library's restore drain.
const PLAYBACK_SAVE_TIMEOUT_MS = 15_000;

export interface PersistPlaybackOptions {
  bookId?: number | null;
  bookIdentity?: string;
  positionSec?: number;
  force?: boolean;
}

interface UsePlaybackPersistenceArgs {
  apiClient: ApiClient | null;
  library: UseLibraryResult;
  audioRef: RefObject<HTMLAudioElement | null>;
  loadedBookId: number | null;
  /** The player's loaded book, updated together with the audio element's source. */
  loadedBookIdRef?: RefObject<number | null>;
  positionOffsetRef?: RefObject<number>;
  loadedChapterNumberRef?: RefObject<number | null>;
}

interface PlaybackPersistence {
  /** Saves for the loaded book keep failing; cleared by its next successful save. */
  saveFailing: boolean;
  persistBookPlaybackPosition: (
    bookId: number,
    positionSec: number,
    options?: PersistPlaybackOptions,
  ) => Promise<number | null>;
  persistLoadedBookPlaybackPosition: (options?: PersistPlaybackOptions) => Promise<number | null>;
  restorePendingResumePosition: (audio: HTMLAudioElement | null, expectedBookId?: number | null) => void;
  waitForAudioReady: (audio: HTMLAudioElement | null) => Promise<void>;
  setPendingResume: (bookId: number, positionSec: number, bookIdentity: string, detail?: LibraryBookDetail) => void;
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
  loadedBookIdRef: playerLoadedBookIdRef,
  positionOffsetRef,
  loadedChapterNumberRef,
}: UsePlaybackPersistenceArgs): PlaybackPersistence {
  const pendingResumeBookIdRef = useRef<number | null>(null);
  const pendingResumePositionSecRef = useRef(0);
  const lastSavedPlaybackRef = useRef<Map<number, { positionSec: number; token: number }>>(new Map());
  const saveSequenceRef = useRef(0);
  const saveTailsRef = useRef<Map<number, Promise<number | null>>>(new Map());
  const consecutiveSaveFailuresRef = useRef<Map<number, number>>(new Map());
  const [saveFailingBookId, setSaveFailingBookId] = useState<number | null>(null);
  const bookIdentityByIdRef = useRef<Map<number, string>>(new Map());
  const playbackDetailByIdRef = useRef<Map<number, LibraryBookDetail>>(new Map());
  const libraryGeneration = library.libraryGeneration ?? 0;
  const session = library.session;
  const playbackGenerationRef = useRef(libraryGeneration);
  const playbackClientIdentityRef = useRef(apiClient);
  if (playbackClientIdentityRef.current !== apiClient || playbackGenerationRef.current !== libraryGeneration) {
    playbackGenerationRef.current = libraryGeneration;
    pendingResumeBookIdRef.current = null;
    pendingResumePositionSecRef.current = 0;
    playbackClientIdentityRef.current = apiClient;
    lastSavedPlaybackRef.current.clear();
    consecutiveSaveFailuresRef.current.clear();
    if (saveFailingBookId !== null) setSaveFailingBookId(null);
    saveTailsRef.current.clear();
    bookIdentityByIdRef.current.clear();
    playbackDetailByIdRef.current.clear();
  }
  const selectedBookIdRef = useRef(library.selectedBookId);
  useLayoutEffect(() => {
    selectedBookIdRef.current = library.selectedBookId;
  });
  // React state trails the audio element while a new book loads, so the
  // player's ref, when given, decides which book the element is playing.
  const committedLoadedBookIdRef = useRef(loadedBookId);
  useLayoutEffect(() => {
    committedLoadedBookIdRef.current = loadedBookId;
  });
  const loadedBookIdRef = playerLoadedBookIdRef ?? committedLoadedBookIdRef;

  const setPendingResume = useCallback((bookId: number, positionSec: number, bookIdentity: string, detail?: LibraryBookDetail) => {
    if (session && !session.isCurrent(libraryGeneration)) return;
    bookIdentityByIdRef.current.set(bookId, bookIdentity);
    if (detail) playbackDetailByIdRef.current.set(bookId, detail);
    pendingResumeBookIdRef.current = bookId;
    pendingResumePositionSecRef.current = detail
      ? readPlaybackResumePosition(detail)
      : readPlaybackCheckpoint(bookId, bookIdentity) ?? positionSec;
  }, [session, libraryGeneration]);

  const clearPendingResume = useCallback(() => {
    pendingResumeBookIdRef.current = null;
    pendingResumePositionSecRef.current = 0;
  }, []);

  const markPlaybackSaved = useCallback((bookId: number, positionSec: number) => {
    if (session && !session.isCurrent(libraryGeneration)) return;
    const bookIdentity = bookIdentityByIdRef.current.get(bookId);
    if (bookIdentity) {
      writePlaybackCheckpoint(bookId, positionSec, bookIdentity);
    }
    lastSavedPlaybackRef.current.set(bookId, {
      positionSec,
      token: ++saveSequenceRef.current,
    });
  }, [session, libraryGeneration]);

  const persistBookPlaybackPosition = useCallback(
    async (bookId: number, positionSec: number, options: PersistPlaybackOptions = {}): Promise<number | null> => {
      const requestApiClient = apiClient;
      if (playbackClientIdentityRef.current !== requestApiClient || (session && !session.isCurrent(libraryGeneration))) {
        return null;
      }

      let bookIdentity = options.bookIdentity ?? bookIdentityByIdRef.current.get(bookId);
      if (!bookIdentity && library.selectedBookDetail?.id === bookId) {
        bookIdentity = playbackCheckpointBookIdentity(library.selectedBookDetail);
        bookIdentityByIdRef.current.set(bookId, bookIdentity);
      }
      const detail = playbackDetailByIdRef.current.get(bookId)
        ?? (library.selectedBookDetail?.id === bookId ? library.selectedBookDetail : null);
      const checkpointPosition = Math.max(0, Number(positionSec) || 0);
      if (bookIdentity) {
        const anchor = detail?.playback_status === "chapter_audio"
          ? chapterPlaybackAnchor(detail.chapters, checkpointPosition,
            loadedBookIdRef.current === bookId ? loadedChapterNumberRef?.current : null)
          : null;
        writePlaybackCheckpoint(bookId, checkpointPosition, bookIdentity, undefined, anchor);
      }
      if (!requestApiClient || !bookId) {
        return null;
      }

      const ownsCurrentClient = () => playbackClientIdentityRef.current === requestApiClient
        && (!session || session.isCurrent(libraryGeneration));
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
      const operation = () => previousTail.catch(() => null).then(async () => {
        if (!ownsCurrentClient()) {
          return null;
        }
        try {
          const response = await requestApiClient.updateBookPlaybackPosition(bookId, normalizedPosition, {
            expectedEpoch: detail?.library_epoch,
            timeoutMs: PLAYBACK_SAVE_TIMEOUT_MS,
          });
          if (!ownsCurrentClient()) {
            return null;
          }
          const savedPosition = Number(response.position_sec);
          const resolved = Number.isFinite(savedPosition) ? savedPosition : normalizedPosition;
          const currentMarker = lastSavedPlaybackRef.current.get(bookId);
          if (currentMarker?.token === token) {
            lastSavedPlaybackRef.current.set(bookId, { positionSec: resolved, token });
            if (selectedBookIdRef.current === bookId) {
              library.setSelectedPlaybackPosition(resolved);
            }
          }
          consecutiveSaveFailuresRef.current.delete(bookId);
          setSaveFailingBookId((current) => (current === bookId ? null : current));
          return resolved;
        } catch (error) {
          if (!ownsCurrentClient()) {
            return null;
          }
          if (isLibraryReplacedError(error)) {
            await library.reloadLibrarySession?.();
            return null;
          }
          // The marker stays at the failed position, so the next automatic
          // save retries one save interval later rather than on every tick.
          const failureCount = (consecutiveSaveFailuresRef.current.get(bookId) ?? 0) + 1;
          consecutiveSaveFailuresRef.current.set(bookId, failureCount);
          // The playing book reports its failures even while another book is viewed.
          const ownsCurrentPlayback = loadedBookIdRef.current === bookId;
          if (ownsCurrentPlayback && failureCount >= PLAYBACK_SAVE_FAILURE_NOTICE_THRESHOLD) {
            setSaveFailingBookId(bookId);
          }
          return null;
        }
      });
      let trackedTail!: Promise<number | null>;
      const pending = session ? session.runMutation(libraryGeneration, operation) : operation();
      trackedTail = pending.finally(() => {
        if (saveTailsRef.current.get(bookId) === trackedTail) {
          saveTailsRef.current.delete(bookId);
        }
      });
      saveTailsRef.current.set(bookId, trackedTail);
      return trackedTail;
    },
    [apiClient, library, libraryGeneration, session, loadedChapterNumberRef],
  );

  const persistLoadedBookPlaybackPosition = useCallback(
    async (options: PersistPlaybackOptions = {}): Promise<number | null> => {
      const currentLoadedBookId = loadedBookIdRef.current;
      const bookId = options.bookId ?? currentLoadedBookId;
      if (!bookId) {
        return null;
      }

      const audio = audioRef.current;
      const hasExplicitPosition = typeof options.positionSec === "number";
      const ownsLoadedAudio = Boolean(
        audio &&
        currentLoadedBookId === bookId &&
        audio.src &&
        bookIdentityByIdRef.current.has(bookId)
      );
      if (!hasExplicitPosition && !ownsLoadedAudio) {
        return null;
      }
      // A freshly loaded source reads 0 until its resume position is applied.
      if (!hasExplicitPosition && pendingResumeBookIdRef.current === bookId && audio!.readyState < 1) {
        return null;
      }
      const resolvedPosition = hasExplicitPosition
        ? options.positionSec!
        : Number(audio!.currentTime || 0) + (positionOffsetRef?.current ?? 0);

      return persistBookPlaybackPosition(bookId, resolvedPosition, options);
    },
    [
      audioRef,
      loadedBookIdRef,
      persistBookPlaybackPosition,
      positionOffsetRef,
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
      if (session && !session.isCurrent(libraryGeneration)) return;
      const resolvedBookId = expectedBookId ?? loadedBookIdRef.current;
      if (!audio || pendingResumeBookIdRef.current !== resolvedBookId) {
        return;
      }

      const target = Math.max(0, Number(pendingResumePositionSecRef.current || 0) - (positionOffsetRef?.current ?? 0));
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
    [loadedBookIdRef, positionOffsetRef, session, libraryGeneration],
  );

  useEffect(() => {
    const onBeforeUnload = () => {
      void persistLoadedBookPlaybackPosition({ force: true });
    };

    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
    };
  }, [persistLoadedBookPlaybackPosition]);

  return {
    saveFailing: saveFailingBookId !== null && saveFailingBookId === loadedBookId,
    persistBookPlaybackPosition,
    persistLoadedBookPlaybackPosition,
    restorePendingResumePosition,
    waitForAudioReady,
    setPendingResume,
    clearPendingResume,
    markPlaybackSaved,
  };
}

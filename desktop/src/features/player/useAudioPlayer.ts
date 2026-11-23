import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { ApiError, isLibraryReplacedError, type ApiClient } from "../../api/client";
import { useGenerationGuard, useMountedRef } from "../../state/generationGuard";
import { formatClock } from "../../format/time";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail, LibraryChapterDetail } from "../../types/api";
import { chapterSetIdentity, chapterStartSeconds, createChapterTimeline, playableChapterForPosition } from "./chapterPositionModel";
import { useAudioElementBindings } from "./useAudioElementBindings";
import { chapterDisplayLabel } from "./chapterPlaybackLabel";
import { playbackCheckpointBookIdentity, readPlaybackResumePosition } from "./playbackCheckpoint";
import { usePlaybackPersistence } from "./playbackPersistence";
import { useSleepTimer } from "./usePlaybackExtras";
import { resolvePlayerCanSeek, resolvePlayerDurationSec, seekAudioIfReady } from "./playerDurationModel";

// Audio authorization only mints a local token; bound it separately from
// loading or streaming the audio so Play can be retried after a stalled read.
const AUDIO_TOKEN_REQUEST_TIMEOUT_MS = 8_000;
const VOLUME_STORAGE_KEY = "orpheus.playerVolume";
// Automatic reloads after media errors, per book, until it plays again. A
// source that keeps failing stays failed until the user presses Play.
const MAX_PLAYBACK_RECOVERY_ATTEMPTS = 3;

function readStoredVolume(): number {
  try {
    const stored = window.localStorage.getItem(VOLUME_STORAGE_KEY);
    const value = stored === null ? Number.NaN : Number(stored);
    return Number.isFinite(value) ? Math.max(0, Math.min(100, Math.round(value))) : 80;
  } catch {
    return 80;
  }
}
const BACKEND_UNAVAILABLE_PLAYBACK = "Can't play: the backend is not responding. Reconnecting…";

interface UseAudioPlayerArgs {
  apiClient: ApiClient | null;
  library: UseLibraryResult;
}

export function useAudioPlayer({ apiClient, library }: UseAudioPlayerArgs) {
  const session = library.session;
  const libraryGeneration = library.libraryGeneration ?? 0;
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [audioSrc, setAudioSrc] = useState<string | null>(null);
  const [loadedBookId, setLoadedBookId] = useState<number | null>(null);
  const loadedBookIdRef = useRef<number | null>(null);
  const [playerDismissedBookId, setPlayerDismissedBookId] = useState<number | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [elapsedSec, setElapsedSec] = useState(0);
  const [totalSec, setTotalSec] = useState(0);
  const [volume, setVolume] = useState(readStoredVolume);
  useEffect(() => {
    try {
      window.localStorage.setItem(VOLUME_STORAGE_KEY, String(volume));
    } catch {
      // Storage can be unavailable; the volume still applies for this session.
    }
  }, [volume]);
  const [playerTitle, setPlayerTitle] = useState("No audio loaded");
  const [playbackError, setPlaybackError] = useState("");
  // The book whose end was reached; it reads "Finished" until it is played again.
  const [finishedBookId, setFinishedBookId] = useState<number | null>(null);
  const [loadedChapterNumber, setLoadedChapterNumber] = useState<number | null>(null);
  const loadedChapterNumberRef = useRef<number | null>(null);
  const positionOffsetRef = useRef(0);
  const loadedPlaybackStatusRef = useRef<LibraryBookDetail["playback_status"] | null>(null);
  // The player follows the book in the audio element; browsing the library
  // changes only the viewed (selected) book. With nothing loaded, the player
  // offers the viewed book.
  const [playingDetail, setPlayingDetail] = useState<LibraryBookDetail | null>(null);
  const playbackDetail = loadedBookId === null
    ? library.selectedBookDetail
    : library.selectedBookDetail?.id === loadedBookId ? library.selectedBookDetail : playingDetail;
  const playbackBookId = loadedBookId ?? library.selectedBookId;
  const playbackPlayable = loadedBookId !== null || library.selectedBookPlayable;
  useEffect(() => {
    const viewed = library.selectedBookDetail;
    if (viewed && viewed.id === loadedBookIdRef.current) setPlayingDetail(viewed);
  }, [library.selectedBookDetail]);
  const chapterSet = useMemo(() => chapterSetIdentity(playbackDetail?.chapters ?? []),
    [playbackDetail?.chapters]);
  const previousPlaybackRef = useRef({ id: playbackDetail?.id, status: playbackDetail?.playback_status, chapterSet });
  const chapterTimeline = useMemo(() => createChapterTimeline(playbackDetail?.chapters ?? []),
    [playbackDetail?.chapters]);
  const chapterPlaylist = playbackDetail?.playback_status === "chapter_audio";
  const playableChapters = useMemo(
    () => (playbackDetail?.chapters ?? []).filter((chapter) => chapter.mp3_path),
    [playbackDetail?.chapters],
  );
  const playlistIndex = useMemo(
    () => Math.max(0, playableChapters.findIndex((chapter) => chapter.number === loadedChapterNumber)),
    [loadedChapterNumber, playableChapters],
  );

  // The load generation and latest selection guard loadBookAudio against
  // rapid next/previous: an older in-flight load must never set src/state late.
  const loadGuard = useGenerationGuard();
  const navigationGuard = useGenerationGuard();
  const [pendingNavigation, setPendingNavigation] = useState<{
    detail: LibraryBookDetail; client: ApiClient | null; owns: () => boolean;
  } | null>(null);
  const playbackRecoveryAttemptsRef = useRef<Map<number, symbol>>(new Map());
  const playbackRecoveryCountRef = useRef<Map<number, number>>(new Map());
  const clearPlaybackRecovery = useCallback(() => {
    playbackRecoveryAttemptsRef.current.clear();
    playbackRecoveryCountRef.current.clear();
  }, []);
  const apiClientLatestRef = useRef(apiClient);
  useLayoutEffect(() => {
    apiClientLatestRef.current = apiClient;
  });
  const mountedRef = useMountedRef(true);
  useEffect(() => () => {
    loadGuard.invalidate();
    navigationGuard.invalidate();
    clearPlaybackRecovery();
  }, []);
  const previousApiClientRef = useRef(apiClient);
  const selectedBookIdLatestRef = useRef<number | null>(library.selectedBookId);
  useLayoutEffect(() => {
    selectedBookIdLatestRef.current = library.selectedBookId;
  });

  const playerEligible = Boolean(playbackBookId && playbackPlayable);
  const playerVisible = Boolean(playerEligible && playbackBookId !== playerDismissedBookId);
  // Before a book is loaded, show where it will resume rather than 00:00.
  const restingPositionSec = useMemo(
    () => (loadedBookId === null && playbackDetail && playbackDetail.playback_status !== "chapter_audio"
      ? readPlaybackResumePosition(playbackDetail) : null),
    [loadedBookId, playbackDetail],
  );
  const displayElapsedSec = restingPositionSec ?? elapsedSec;
  const currentChapter = useMemo(
    () => chapterPlaylist
      ? playableChapters.find((chapter) => chapter.number === loadedChapterNumber) ?? playableChapters[0] ?? null
      : chapterTimeline.at(displayElapsedSec),
    [chapterPlaylist, chapterTimeline, displayElapsedSec, loadedChapterNumber, playableChapters],
  );
  // An audiobook's transport steps through its chapters; only a book without
  // chapters steps through the library.
  const playbackChapters = playbackDetail?.chapters ?? [];
  const chapterTransport = chapterPlaylist || playbackChapters.length > 1;
  const transportChapterIndex = chapterPlaylist
    ? playlistIndex
    : playbackChapters.findIndex((chapter) => chapter.number === currentChapter?.number);
  const transportChapters = chapterPlaylist ? playableChapters : playbackChapters;
  // Library steps start from the playing book, not from the one being viewed.
  const playbackBookIndex = loadedBookId === null
    ? library.selectedBookIndex
    : library.items.findIndex((item) => item.id === loadedBookId);
  const canGoPrevious = chapterTransport ? transportChapterIndex > 0 : playbackBookIndex > 0;
  const canGoNext = chapterTransport
    ? transportChapterIndex >= 0 && transportChapterIndex < transportChapters.length - 1
    : playbackBookIndex >= 0 &&
      (playbackBookIndex < library.items.length - 1 || library.hasMore);
  const currentChapterLabel = chapterDisplayLabel(currentChapter);
  const sleep = useSleepTimer(audioRef, {
    bookKey: JSON.stringify([libraryGeneration, playbackBookId, playbackDetail?.library_epoch]),
    chapterAt: (positionSec) => {
      const chapters = playbackDetail?.chapters ?? [];
      const position = loadedBookIdRef.current === playbackBookId ? positionSec
        : playbackDetail ? readPlaybackResumePosition(playbackDetail) : 0;
      const chapter = chapterPlaylist
        ? chapters.find((item) => item.number === loadedChapterNumberRef.current) ?? currentChapter
        : chapterTimeline.at(position);
      const index = chapters.findIndex((item) => item.number === chapter?.number);
      const nextChapter = chapters[index + 1];
      return {
        number: chapter?.number ?? null,
        // Separate chapter files and the final packaged chapter stop on ended.
        endSec: !chapterPlaylist && index >= 0 && nextChapter
          ? chapterStartSeconds(chapters, nextChapter.number) : null,
      };
    },
  });
  const bookDurationMs = useMemo(() => {
    const chapters = playbackDetail?.chapters ?? [];
    if (chapters.length === 0) {
      return null;
    }
    return chapters.reduce((total, chapter) => total + Math.max(0, Number(chapter.duration_ms || 0)), 0);
  }, [playbackDetail?.chapters]);
  const durationSec = resolvePlayerDurationSec({
    audioDurationSec: totalSec,
    bookDurationMs: chapterPlaylist ? null : bookDurationMs,
    // Secondary only: whole-book sum is preferred for the packaged stream slider.
    chapterDurationMs: !chapterPlaylist && bookDurationMs && bookDurationMs > 0 ? null : (currentChapter?.duration_ms ?? null),
  });
  const canSeek = resolvePlayerCanSeek({ audioSrc, loadedBookId });
  const updatePlayerStatus = useCallback(() => {
    const audio = audioRef.current;

    if (!playerEligible) {
      setPlayerTitle("No audio loaded");
      return;
    }

    if (!audio || !audio.src || loadedBookId === null) {
      setPlayerTitle(finishedBookId !== null && finishedBookId === playbackBookId ? "Finished" : "Ready to play");
      return;
    }

    if (audio.ended) {
      setPlayerTitle("Finished");
      return;
    }

    setPlayerTitle(`${audio.paused ? "Paused" : "Playing"} · ${formatClock(audio.currentTime || 0)}`);
  }, [finishedBookId, loadedBookId, playbackBookId, playerEligible]);

  const {
    saveFailing,
    persistLoadedBookPlaybackPosition,
    restorePendingResumePosition,
    waitForAudioReady,
    setPendingResume,
    clearPendingResume,
    markPlaybackSaved,
  } = usePlaybackPersistence({
    apiClient,
    library,
    audioRef,
    loadedBookId,
    loadedBookIdRef,
    positionOffsetRef,
    loadedChapterNumberRef,
  });

  const clearAudioSource = useCallback(() => {
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    }

    setAudioSrc(null);
    setPlaybackError("");
    loadedBookIdRef.current = null;
    loadedPlaybackStatusRef.current = null;
    setLoadedBookId(null);
    setPlayingDetail(null);
    loadedChapterNumberRef.current = null;
    setLoadedChapterNumber(null);
    positionOffsetRef.current = 0;
    clearPendingResume();
    setIsPlaying(false);
    setElapsedSec(0);
    setTotalSec(0);
    updatePlayerStatus();
  }, [clearPendingResume, updatePlayerStatus]);

  const finishLoadedBook = useCallback(() => {
    const bookId = loadedBookIdRef.current;
    // A load started before the book ended must not restart it.
    loadGuard.invalidate();
    clearAudioSource();
    setFinishedBookId(bookId);
  }, [clearAudioSource]);

  const handleAudioMetadata = useCallback(
    (expectedBookId?: number | null) => {
      const audio = audioRef.current;
      if (!audio) {
        return;
      }

      restorePendingResumePosition(audio, expectedBookId);
      setTotalSec(Number.isFinite(audio.duration) && audio.duration > 0 ? Math.floor(audio.duration) : 0);
      setElapsedSec(Math.max(0, Number(audio.currentTime) || 0));
      updatePlayerStatus();
    },
    [restorePendingResumePosition, updatePlayerStatus],
  );

  const loadBookAudio = useCallback(
    async (
      bookId: number | null,
      detail: LibraryBookDetail | null,
      autoPlay: boolean,
      forceReload = false,
      requestedChapter?: LibraryChapterDetail,
    ): Promise<boolean> => {
      if (mountedRef.current && !apiClient && bookId && detail?.playback_url) {
        setPlaybackError(BACKEND_UNAVAILABLE_PLAYBACK);
        return false;
      }
      if (!mountedRef.current || !apiClient || !bookId || !detail?.playback_url || (session && !session.isCurrent(libraryGeneration))) {
        return false;
      }

      const audio = audioRef.current;
      if (!audio) {
        return false;
      }

      setPlaybackError("");
      loadGuard.invalidate();
      const ownsLoad = loadGuard.capture();
      const requestApiClient = apiClient;
      // Browsing to another book does not cancel a load; only a newer load,
      // a replaced client or library, or unmounting does.
      const isStale = () =>
        !mountedRef.current ||
        (session && !session.isCurrent(libraryGeneration)) ||
        !ownsLoad() ||
        apiClientLatestRef.current !== requestApiClient;

      const playlist = detail.playback_status === "chapter_audio";
      const resumePosition = readPlaybackResumePosition(detail);
      const available = detail.chapters.filter((chapter) => chapter.mp3_path);
      const chapter = playlist ? requestedChapter
        ?? (loadedBookIdRef.current === bookId ? available.find((item) => item.number === loadedChapterNumberRef.current) : null)
        ?? playableChapterForPosition(detail.chapters, resumePosition) : null;
      if (playlist && !chapter?.mp3_path) {
        setPlaybackError("Playback failed: chapter audio is unavailable");
        return false;
      }

      if (forceReload || loadedBookIdRef.current !== bookId || !audio.src || loadedChapterNumberRef.current !== (chapter?.number ?? null)) {
        // Save where the outgoing audio stopped, whether it is this book or the
        // one playback switches away from.
        if (loadedBookIdRef.current !== null && audio.src) {
          void persistLoadedBookPlaybackPosition({ force: true });
        }
        // All callers invoke playback actions fire-and-forget (`void ...`), so
        // a rejection here would surface as an unhandled rejection and a dead
        // play button. Fail soft: report and bail instead of throwing.
        let audioUrl: string;
        try {
          audioUrl = await apiClient.getLibraryAudioUrl(bookId, { expectedEpoch: detail.library_epoch, timeoutMs: AUDIO_TOKEN_REQUEST_TIMEOUT_MS });
        } catch (error) {
          if (isLibraryReplacedError(error) && apiClientLatestRef.current === requestApiClient
            && (!session || session.isCurrent(libraryGeneration))) {
            await library.reloadLibrarySession?.();
          } else if (!isStale()) {
            setPlaybackError(error instanceof ApiError && error.code === "network" ? BACKEND_UNAVAILABLE_PLAYBACK : "Playback failed");
          }
          return false;
        }
        if (isStale()) {
          return false;
        }

        let nextAudioUrl: URL;
        try {
          nextAudioUrl = new URL(audioUrl);
        } catch {
          if (!isStale()) {
            setPlaybackError("Playback failed");
          }
          return false;
        }
        nextAudioUrl.searchParams.set("v", String(Date.now()));
        if (chapter) nextAudioUrl.searchParams.set("chapter_number", String(chapter.number));

        setPendingResume(
          bookId,
          Number(detail.playback_position_sec || 0),
          playbackCheckpointBookIdentity(detail),
          detail,
        );
        // A chapter jump seeks to the chapter itself, not to the saved position.
        if (requestedChapter) clearPendingResume();
        audio.crossOrigin = "anonymous";
        positionOffsetRef.current = chapter ? chapterStartSeconds(detail.chapters, chapter.number, false) : 0;
        loadedChapterNumberRef.current = chapter?.number ?? null;
        setLoadedChapterNumber(chapter?.number ?? null);
        loadedBookIdRef.current = bookId;
        loadedPlaybackStatusRef.current = detail.playback_status;
        setLoadedBookId(bookId);
        setPlayingDetail(detail);
        setFinishedBookId(null);
        setAudioSrc(nextAudioUrl.toString());
        audio.src = nextAudioUrl.toString();
        audio.load();
        setTotalSec(0);

        await waitForAudioReady(audio);
        if (isStale()) {
          return false;
        }
        restorePendingResumePosition(audio, bookId);
        handleAudioMetadata(bookId);
      }

      if (autoPlay) {
        if (isStale()) {
          return false;
        }
        try {
          await audio.play();
        } catch {
          if (!isStale()) setPlaybackError("Playback failed");
          return false;
        }
      }
      return !isStale();
    },
    [
      apiClient,
      session,
      libraryGeneration,
      library.reloadLibrarySession,
      handleAudioMetadata,
      loadedBookId,
      restorePendingResumePosition,
      setPendingResume,
      clearPendingResume,
      waitForAudioReady,
      persistLoadedBookPlaybackPosition,
    ],
  );

  const loadSelectedBookAudio = useCallback(
    async (autoPlay: boolean): Promise<void> => {
      await loadBookAudio(library.selectedBookId, library.selectedBookDetail, autoPlay);
    },
    [library.selectedBookDetail, library.selectedBookId, loadBookAudio],
  );

  const playRelative = useCallback(
    async (delta: number, fromBookId: number | null = loadedBookIdRef.current): Promise<void> => {
      if (!library.items.length || delta === 0) {
        return;
      }
      navigationGuard.invalidate();
      const owns = navigationGuard.capture();

      if (fromBookId === null && library.selectedBookIndex < 0) {
        const detail = await library.selectBook(0);
        if (!detail || !owns() || !mountedRef.current) return;
        setPlayerDismissedBookId(null);
        setPendingNavigation({ detail, client: apiClient, owns });
        return;
      }

      const detail = await library.selectRelative(delta, fromBookId);
      if (!detail || !owns() || !mountedRef.current) return;
      setPlayerDismissedBookId(null);
      setPendingNavigation({ detail, client: apiClient, owns });
    },
    [apiClient, library],
  );

  // Selection commits before audio loading so its ownership checks see the
  // new book even when a page/detail response resolves in the same React batch.
  useEffect(() => {
    if (!pendingNavigation) return;
    setPendingNavigation(null);
    if (pendingNavigation.owns() && pendingNavigation.client === apiClient
      && library.selectedBookId === pendingNavigation.detail.id) {
      void loadBookAudio(pendingNavigation.detail.id, pendingNavigation.detail, true);
    }
  }, [apiClient, library.selectedBookId, loadBookAudio, pendingNavigation]);

  /** The transport's play/pause: the loaded book, or the viewed book when none is loaded. */
  const togglePlay = useCallback(async (): Promise<void> => {
    if (!playbackBookId || !playerEligible) {
      return;
    }

    if (playerDismissedBookId === playbackBookId) {
      setPlayerDismissedBookId(null);
    }

    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    if (loadedBookId === null || !audio.src) {
      await loadSelectedBookAudio(true);
      return;
    }

    // After automatic recovery gave up, Play reloads the failed source.
    if (audio.error && playbackDetail?.id === loadedBookId) {
      playbackRecoveryCountRef.current.delete(loadedBookId);
      await loadBookAudio(loadedBookId, playbackDetail, true, true);
      return;
    }

    if (audio.paused) {
      setPlaybackError("");
      try {
        await audio.play();
      } catch {
        setPlaybackError("Playback failed");
      }
      return;
    }

    audio.pause();
  }, [
    playbackBookId,
    playbackDetail,
    loadBookAudio,
    loadSelectedBookAudio,
    loadedBookId,
    playerDismissedBookId,
    playerEligible,
  ]);

  /** Play on a viewed book: toggles it when it is the loaded book, otherwise switches playback to it. */
  const playSelectedBook = useCallback(async (): Promise<void> => {
    const audio = audioRef.current;
    if (library.selectedBookId !== null && library.selectedBookId === loadedBookId && audio?.src) {
      await togglePlay();
      return;
    }
    if (!library.selectedBookId || !library.selectedBookPlayable) return;
    setPlayerDismissedBookId(null);
    await loadSelectedBookAudio(true);
  }, [library.selectedBookId, library.selectedBookPlayable, loadSelectedBookAudio, loadedBookId, togglePlay]);

  const seekBy = useCallback(
    (deltaSeconds: number): void => {
      const audio = audioRef.current;
      const positionSeconds = Math.max(0, (audio?.currentTime || 0) + deltaSeconds);
      if (!seekAudioIfReady({ audio, loadedBookId, positionSeconds })) {
        return;
      }

      setElapsedSec(Math.max(0, Number(audio!.currentTime) || 0));
      sleep.onSeeked();
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true });
    },
    [
      loadedBookId,
      persistLoadedBookPlaybackPosition,
      updatePlayerStatus,
      sleep.onSeeked,
    ],
  );

  const seekTo = useCallback(
    (positionSeconds: number): void => {
      const audio = audioRef.current;
      if (!seekAudioIfReady({ audio, loadedBookId, positionSeconds })) {
        return;
      }

      setElapsedSec(Math.max(0, Number(audio!.currentTime) || 0));
      sleep.onSeeked();
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true });
    },
    [
      loadedBookId,
      persistLoadedBookPlaybackPosition,
      updatePlayerStatus,
      sleep.onSeeked,
    ],
  );

  const jumpToChapter = useCallback(
    async (chapter: LibraryChapterDetail): Promise<void> => {
      // Now Playing lists the playing book's chapters, an album the viewed
      // book's; chapter ids are unique, so the chapter names its book.
      const requestedDetail = playbackDetail?.chapters.some((item) => item.id === chapter.id)
        ? playbackDetail
        : library.selectedBookDetail;
      const requestedBookId = requestedDetail?.id ?? null;
      if (!requestedBookId || !requestedDetail?.playback_url) {
        return;
      }

      setPlayerDismissedBookId(null);
      const loading = loadBookAudio(requestedBookId, requestedDetail, false, false, chapter);
      const ownsLoad = loadGuard.capture();
      const loaded = await loading;

      if (!loaded || !ownsLoad() || loadedBookIdRef.current !== requestedBookId) {
        return;
      }

      const target = requestedDetail.playback_status === "chapter_audio" ? 0 : chapterStartSeconds(requestedDetail.chapters, chapter.number);
      const audio = audioRef.current;
      if (!audio || !audio.src) {
        return;
      }

      const duration = Number.isFinite(audio.duration) ? audio.duration : target;
      try {
        audio.currentTime = Math.max(0, Math.min(duration || target, target));
        sleep.onSeeked();
        await audio.play();
      } catch {
        if (ownsLoad()) setPlaybackError("Playback failed");
        return;
      }
      if (!ownsLoad()) return;
      setElapsedSec(Math.max(0, Number(audio.currentTime) || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({
        bookId: requestedBookId,
        positionSec: audio.currentTime + positionOffsetRef.current,
        force: true,
      });
    },
    [
      library.selectedBookDetail,
      loadBookAudio,
      persistLoadedBookPlaybackPosition,
      playbackDetail,
      updatePlayerStatus,
      sleep.onSeeked,
    ],
  );

  // Every position save replaces the selected detail, which re-creates
  // jumpToChapter. Chapter lists bind this stable entry point instead, so a
  // save does not re-render each of a long book's chapter rows.
  const jumpToChapterRef = useRef(jumpToChapter);
  useLayoutEffect(() => {
    jumpToChapterRef.current = jumpToChapter;
  }, [jumpToChapter]);
  const jumpToLatestChapter = useCallback(
    (chapter: LibraryChapterDetail): Promise<void> => jumpToChapterRef.current(chapter),
    [],
  );

  const playChapterRelative = useCallback(async (delta: number): Promise<void> => {
    const next = transportChapters[transportChapterIndex + delta];
    if (transportChapterIndex >= 0 && next) await jumpToChapter(next);
  }, [jumpToChapter, transportChapterIndex, transportChapters]);

  const setPlayerVolume = useCallback((nextVolume: number): void => {
    const clamped = Math.max(0, Math.min(100, Math.round(nextVolume)));
    setVolume(clamped);

    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    audio.volume = clamped / 100;
    audio.muted = clamped === 0;
    updatePlayerStatus();
  }, [updatePlayerStatus]);

  const closePlayer = useCallback(() => {
    navigationGuard.invalidate();
    loadGuard.invalidate();
    clearPendingResume();
    if (playbackBookId) {
      setPlayerDismissedBookId(playbackBookId);
    }

    const audio = audioRef.current;
    if (audio && !audio.paused) {
      audio.pause();
    }

    void persistLoadedBookPlaybackPosition({ force: true });
  }, [clearPendingResume, playbackBookId, persistLoadedBookPlaybackPosition]);

  const releaseBookAudio = useCallback((bookId: number) => {
    if (selectedBookIdLatestRef.current === bookId || loadedBookIdRef.current === bookId) {
      navigationGuard.invalidate();
      loadGuard.invalidate();
    }
    if (loadedBookIdRef.current !== bookId) return;
    void persistLoadedBookPlaybackPosition({ force: true });
    clearAudioSource();
  }, [clearAudioSource, persistLoadedBookPlaybackPosition]);

  useEffect(() => session?.subscribe(() => {
    navigationGuard.invalidate();
    loadGuard.invalidate();
    clearPlaybackRecovery();
    setPlayerDismissedBookId(null);
    const bookId = loadedBookIdRef.current;
    if (bookId !== null) releaseBookAudio(bookId);
    else clearAudioSource();
  }), [session, releaseBookAudio, clearAudioSource, clearPlaybackRecovery]);

  const handlePlaybackError = useCallback(() => {
    setPlaybackError("Playback failed");
    const bookId = loadedBookIdRef.current ?? playbackBookId;
    const detail = playbackDetail?.id === bookId ? playbackDetail : null;
    const audio = audioRef.current;
    if (!bookId || !detail?.playback_url || !audio) {
      return;
    }
    if (playbackRecoveryAttemptsRef.current.has(bookId)) {
      return;
    }
    const recoveryCount = playbackRecoveryCountRef.current.get(bookId) ?? 0;
    if (recoveryCount >= MAX_PLAYBACK_RECOVERY_ATTEMPTS) {
      return;
    }
    playbackRecoveryCountRef.current.set(bookId, recoveryCount + 1);

    const recoveryToken = Symbol("playback-recovery");
    playbackRecoveryAttemptsRef.current.set(bookId, recoveryToken);
    setPendingResume(
      bookId,
      Number(audio.currentTime || detail.playback_position_sec || 0),
      playbackCheckpointBookIdentity(detail),
    );
    setPlayerTitle("Reconnecting playback…");
    // pause() takes effect before its queued media event updates React. Read
    // the element so recovery cannot undo a Pause pressed just before an error.
    void loadBookAudio(bookId, detail, !audio.paused, true).finally(() => {
      if (playbackRecoveryAttemptsRef.current.get(bookId) === recoveryToken) {
        playbackRecoveryAttemptsRef.current.delete(bookId);
      }
    });
  }, [playbackBookId, playbackDetail, loadBookAudio, setPendingResume]);

  const markPlaybackHealthy = useCallback(() => {
    const bookId = loadedBookIdRef.current ?? selectedBookIdLatestRef.current;
    if (bookId !== null) {
      playbackRecoveryAttemptsRef.current.delete(bookId);
      // Loading and resume seeks also tick while paused; only playback proves the source works.
      if (audioRef.current && !audioRef.current.paused) playbackRecoveryCountRef.current.delete(bookId);
    }
  }, []);

  useEffect(() => {
    if (previousApiClientRef.current === apiClient) {
      return;
    }
    previousApiClientRef.current = apiClient;
    navigationGuard.invalidate();
    loadGuard.invalidate();
    clearPlaybackRecovery();
    clearAudioSource();
  }, [apiClient, clearAudioSource, clearPlaybackRecovery]);

  // Only the loaded book losing its audio (for example a repair rebuilding it)
  // unloads the player; browsing to an unplayable book does not.
  const loadedBookUnplayable = loadedBookId !== null && playbackDetail?.id === loadedBookId && !playbackDetail.playback_url;
  useEffect(() => {
    if (loadedBookUnplayable) clearAudioSource();
    updatePlayerStatus();
  }, [clearAudioSource, loadedBookUnplayable, updatePlayerStatus]);
  // Media handlers refresh the status as they update time and play state. A
  // second pass after every tick would re-render the whole dashboard again.

  // The playing book's audio was rebuilt (status or chapter set changed): reload it.
  useEffect(() => {
    const detail = playbackDetail;
    const previous = previousPlaybackRef.current;
    previousPlaybackRef.current = { id: detail?.id, status: detail?.playback_status, chapterSet };
    if (
      detail && previous.id === detail.id && (previous.status !== detail.playback_status || previous.chapterSet !== chapterSet) &&
      loadedBookIdRef.current === detail.id && audioRef.current?.src
    ) {
      void loadBookAudio(detail.id, detail, !audioRef.current.paused, true);
    }
  }, [chapterSet, playbackDetail, loadBookAudio]);

  const audioProps = useAudioElementBindings({
    stopAtChapterEnd: sleep.stopAtChapterEnd,
    onSeeking: sleep.onSeeking,
    onSeeked: sleep.onSeeked,
    audioRef,
    audioSrc,
    loadedBookId,
    volume,
    library,
    persistLoadedBookPlaybackPosition,
    markPlaybackSaved,
    canFinishBook: loadedPlaybackStatusRef.current === playbackDetail?.playback_status &&
      !(chapterPlaylist && playbackDetail?.chapters.some((chapter) => !chapter.mp3_path)),
    onBookCompleted: finishLoadedBook,
    handleAudioMetadata,
    updatePlayerStatus,
    playRelative,
    advanceChapter: chapterPlaylist ? () => {
      if (playlistIndex >= playableChapters.length - 1) return false;
      void playChapterRelative(1);
      return true;
    } : undefined,
    setIsPlaying,
    setElapsedSec,
    setTotalSec,
    setVolume,
    onPlaybackError: handlePlaybackError,
    onPlaybackHealthy: markPlaybackHealthy,
  });

  return {
    sleepTimer: sleep.sleepTimer,
    sleepRemainingSec: sleep.sleepRemainingSec,
    setSleepTimer: sleep.setSleepTimer,
    audioRef,
    audioProps,
    playerEligible,
    playerVisible,
    /** The book in the audio element, or null. */
    playingBookId: loadedBookId,
    /** The book the player shows: the loaded book, else the viewed one. */
    playbackDetail,
    chapterTransport,
    playing: isPlaying,
    // A failing save outranks the ticking status line, which rewrites the title every second.
    title: playbackError || (saveFailing ? "Could not save playback position" : playerTitle),
    elapsedSec: displayElapsedSec,
    durationSec,
    canSeek,
    volume,
    currentChapter,
    currentChapterLabel,
    chapterPlaylist,
    canGoPrevious,
    canGoNext,
    togglePlay,
    playSelectedBook,
    seekBy,
    seekTo,
    playRelative: chapterTransport ? playChapterRelative : playRelative,
    jumpToChapter: jumpToLatestChapter,
    setPlayerVolume,
    closePlayer,
    releaseBookAudio,
  };
}

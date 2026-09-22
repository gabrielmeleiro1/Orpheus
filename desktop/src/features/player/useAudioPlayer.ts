import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../../api/client";
import { formatClock } from "../../format/time";
import type { UseLibraryResult } from "../library/useLibrary";
import type { LibraryBookDetail, LibraryChapterDetail } from "../../types/api";
import { chapterStartSeconds, currentChapterForPosition } from "./chapterPositionModel";
import { useAudioElementBindings } from "./useAudioElementBindings";
import { playbackCheckpointBookIdentity, readPlaybackCheckpoint } from "./playbackCheckpoint";
import { usePlaybackPersistence } from "./playbackPersistence";
import { resolvePlayerCanSeek, resolvePlayerDurationSec, seekAudioIfReady } from "./playerDurationModel";

interface UseAudioPlayerArgs {
  apiClient: ApiClient | null;
  library: UseLibraryResult;
}

export function useAudioPlayer({ apiClient, library }: UseAudioPlayerArgs) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [audioSrc, setAudioSrc] = useState<string | null>(null);
  const [loadedBookId, setLoadedBookId] = useState<number | null>(null);
  const loadedBookIdRef = useRef<number | null>(null);
  const [playerDismissedBookId, setPlayerDismissedBookId] = useState<number | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [elapsedSec, setElapsedSec] = useState(0);
  const [totalSec, setTotalSec] = useState(0);
  const [volume, setVolume] = useState(80);
  const [playerTitle, setPlayerTitle] = useState("No audio loaded");
  const [playbackError, setPlaybackError] = useState("");
  const [loadedChapterNumber, setLoadedChapterNumber] = useState<number | null>(null);
  const loadedChapterNumberRef = useRef<number | null>(null);
  const positionOffsetRef = useRef(0);
  const chapterPlaylist = library.selectedBookDetail?.playback_status === "chapter_audio";
  const playableChapters = useMemo(
    () => (library.selectedBookDetail?.chapters ?? []).filter((chapter) => chapter.mp3_path),
    [library.selectedBookDetail?.chapters],
  );
  const playlistIndex = Math.max(0, playableChapters.findIndex((chapter) => chapter.number === loadedChapterNumber));

  const previousSelectedBookIdRef = useRef<number | null>(null);
  // Monotonic load id + latest-selection mirror guard loadBookAudio against
  // rapid next/previous: an older in-flight load must never set src/state late.
  const loadSequenceRef = useRef(0);
  const playbackRecoveryAttemptsRef = useRef<Map<number, symbol>>(new Map());
  const apiClientLatestRef = useRef(apiClient);
  apiClientLatestRef.current = apiClient;
  const previousApiClientRef = useRef(apiClient);
  const selectedBookIdLatestRef = useRef<number | null>(library.selectedBookId);
  selectedBookIdLatestRef.current = library.selectedBookId;

  const playerEligible = Boolean(library.selectedBookId && library.selectedBookPlayable);
  const playerVisible = Boolean(playerEligible && library.selectedBookId !== playerDismissedBookId);
  const canGoPrevious = chapterPlaylist ? playlistIndex > 0 : library.selectedBookIndex > 0;
  const canGoNext = chapterPlaylist ? playlistIndex < playableChapters.length - 1 :
    library.selectedBookIndex >= 0 &&
    library.selectedBookIndex < library.items.length - 1;
  const currentChapter = useMemo(
    () => chapterPlaylist
      ? playableChapters.find((chapter) => chapter.number === loadedChapterNumber) ?? playableChapters[0] ?? null
      : currentChapterForPosition(library.selectedBookDetail?.chapters ?? [], elapsedSec),
    [chapterPlaylist, elapsedSec, library.selectedBookDetail?.chapters, loadedChapterNumber, playableChapters],
  );
  const currentChapterLabel = currentChapter
    ? `Chapter ${currentChapter.number}: ${currentChapter.title || "Untitled"}`
    : "Chapter unknown";
  const bookDurationMs = useMemo(() => {
    const chapters = library.selectedBookDetail?.chapters ?? [];
    if (chapters.length === 0) {
      return null;
    }
    return chapters.reduce((total, chapter) => total + Math.max(0, Number(chapter.duration_ms || 0)), 0);
  }, [library.selectedBookDetail?.chapters]);
  const durationSec = resolvePlayerDurationSec({
    audioDurationSec: totalSec,
    bookDurationMs: chapterPlaylist ? null : bookDurationMs,
    // Secondary only: whole-book sum is preferred for the packaged stream slider.
    chapterDurationMs: !chapterPlaylist && bookDurationMs && bookDurationMs > 0 ? null : (currentChapter?.duration_ms ?? null),
  });
  const canSeek = resolvePlayerCanSeek({
    audioSrc,
    loadedBookId,
    selectedBookId: library.selectedBookId,
  });
  const updatePlayerStatus = useCallback(() => {
    const audio = audioRef.current;

    if (!playerEligible) {
      setPlayerTitle("No audio loaded");
      return;
    }

    if (!audio || !audio.src || !library.selectedBookId || loadedBookId !== library.selectedBookId) {
      setPlayerTitle("Ready to play");
      return;
    }

    if (audio.ended) {
      setPlayerTitle("Finished");
      return;
    }

    setPlayerTitle(`${audio.paused ? "Paused" : "Playing"} · ${formatClock(audio.currentTime || 0)}`);
  }, [library.selectedBookId, loadedBookId, playerEligible]);

  const onSaveFailed = useCallback(() => {
    setPlayerTitle("Could not save playback position");
  }, []);

  const {
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
    onSaveFailed,
    positionOffsetRef,
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
    setLoadedBookId(null);
    loadedChapterNumberRef.current = null;
    setLoadedChapterNumber(null);
    positionOffsetRef.current = 0;
    clearPendingResume();
    setIsPlaying(false);
    setElapsedSec(0);
    setTotalSec(0);
    updatePlayerStatus();
  }, [clearPendingResume, updatePlayerStatus]);

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
      if (!apiClient || !bookId || !detail?.playback_url) {
        return false;
      }

      const audio = audioRef.current;
      if (!audio) {
        return false;
      }

      setPlaybackError("");
      const requestId = ++loadSequenceRef.current;
      const requestApiClient = apiClient;
      const isStale = () =>
        loadSequenceRef.current !== requestId ||
        apiClientLatestRef.current !== requestApiClient ||
        selectedBookIdLatestRef.current !== bookId;

      const playlist = detail.playback_status === "chapter_audio";
      const resumePosition = readPlaybackCheckpoint(bookId, playbackCheckpointBookIdentity(detail))
        ?? Number(detail.playback_position_sec || 0);
      const available = detail.chapters.filter((chapter) => chapter.mp3_path);
      const chapter = playlist ? requestedChapter
        ?? (loadedBookIdRef.current === bookId ? available.find((item) => item.number === loadedChapterNumberRef.current) : null)
        ?? currentChapterForPosition(available, resumePosition) : null;
      if (playlist && !chapter?.mp3_path) {
        setPlaybackError("Playback failed: chapter audio is unavailable");
        return false;
      }

      if (forceReload || loadedBookIdRef.current !== bookId || !audio.src || loadedChapterNumberRef.current !== (chapter?.number ?? null)) {
        if (loadedBookIdRef.current === bookId && audio.src) {
          void persistLoadedBookPlaybackPosition({ force: true, silent: true });
        }
        setPendingResume(
          bookId,
          Number(detail.playback_position_sec || 0),
          playbackCheckpointBookIdentity(detail),
        );

        // All callers invoke playback actions fire-and-forget (`void ...`), so
        // a rejection here would surface as an unhandled rejection and a dead
        // play button. Fail soft: report and bail instead of throwing.
        let audioUrl: string;
        try {
          audioUrl = await apiClient.getLibraryAudioUrl(bookId);
        } catch {
          if (!isStale()) {
            setPlaybackError("Playback failed");
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

        audio.crossOrigin = "anonymous";
        positionOffsetRef.current = chapter ? chapterStartSeconds(detail.chapters, chapter.number) : 0;
        loadedChapterNumberRef.current = chapter?.number ?? null;
        setLoadedChapterNumber(chapter?.number ?? null);
        loadedBookIdRef.current = bookId;
        setLoadedBookId(bookId);
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
      handleAudioMetadata,
      loadedBookId,
      restorePendingResumePosition,
      setPendingResume,
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
    async (delta: number): Promise<void> => {
      if (!library.items.length || delta === 0) {
        return;
      }

      if (library.selectedBookIndex < 0) {
        const nextBook = library.items[0];
        const detail = await library.selectBook(0);
        setPlayerDismissedBookId(null);
        await loadBookAudio(nextBook?.id ?? null, detail, true);
        return;
      }

      const nextIndex = library.selectedBookIndex + delta;
      if (nextIndex < 0 || nextIndex >= library.items.length) {
        return;
      }

      const nextBook = library.items[nextIndex];
      const detail = await library.selectBook(nextIndex);
      setPlayerDismissedBookId(null);
      await loadBookAudio(nextBook?.id ?? null, detail, true);
    },
    [library, loadBookAudio],
  );

  const togglePlay = useCallback(async (): Promise<void> => {
    if (!library.selectedBookId || !playerEligible) {
      return;
    }

    if (playerDismissedBookId === library.selectedBookId) {
      setPlayerDismissedBookId(null);
    }

    const audio = audioRef.current;
    if (!audio) {
      return;
    }

    if (loadedBookId !== library.selectedBookId || !audio.src) {
      await loadSelectedBookAudio(true);
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
    library.selectedBookId,
    loadSelectedBookAudio,
    loadedBookId,
    playerDismissedBookId,
    playerEligible,
  ]);

  const seekBy = useCallback(
    (deltaSeconds: number): void => {
      const audio = audioRef.current;
      const positionSeconds = Math.max(0, (audio?.currentTime || 0) + deltaSeconds);
      if (
        !seekAudioIfReady({
          audio,
          loadedBookId,
          selectedBookId: library.selectedBookId,
          positionSeconds,
        })
      ) {
        return;
      }

      setElapsedSec(Math.max(0, Number(audio!.currentTime) || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    },
    [
      library.selectedBookId,
      loadedBookId,
      persistLoadedBookPlaybackPosition,
      updatePlayerStatus,
    ],
  );

  const seekTo = useCallback(
    (positionSeconds: number): void => {
      const audio = audioRef.current;
      if (
        !seekAudioIfReady({
          audio,
          loadedBookId,
          selectedBookId: library.selectedBookId,
          positionSeconds,
        })
      ) {
        return;
      }

      setElapsedSec(Math.max(0, Number(audio!.currentTime) || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({ force: true, silent: true });
    },
    [
      library.selectedBookId,
      loadedBookId,
      persistLoadedBookPlaybackPosition,
      updatePlayerStatus,
    ],
  );

  const jumpToChapter = useCallback(
    async (chapter: LibraryChapterDetail): Promise<void> => {
      const requestedBookId = library.selectedBookId;
      const requestedDetail = library.selectedBookDetail;
      if (!requestedBookId || !requestedDetail || !playerEligible) {
        return;
      }

      setPlayerDismissedBookId(null);
      const loading = loadBookAudio(requestedBookId, requestedDetail, false, false, chapter);
      const requestSequence = loadSequenceRef.current;
      const loaded = await loading;

      if (
        !loaded ||
        loadSequenceRef.current !== requestSequence ||
        selectedBookIdLatestRef.current !== requestedBookId ||
        loadedBookIdRef.current !== requestedBookId
      ) {
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
        await audio.play();
      } catch {
        if (loadSequenceRef.current === requestSequence && selectedBookIdLatestRef.current === requestedBookId) setPlaybackError("Playback failed");
        return;
      }
      if (loadSequenceRef.current !== requestSequence || selectedBookIdLatestRef.current !== requestedBookId) return;
      setElapsedSec(Math.max(0, Number(audio.currentTime) || 0));
      updatePlayerStatus();
      void persistLoadedBookPlaybackPosition({
        bookId: requestedBookId,
        positionSec: audio.currentTime + positionOffsetRef.current,
        force: true,
        silent: true,
      });
    },
    [
      library.selectedBookDetail,
      library.selectedBookId,
      loadBookAudio,
      persistLoadedBookPlaybackPosition,
      playerEligible,
      updatePlayerStatus,
    ],
  );

  const playChapterRelative = useCallback(async (delta: number): Promise<void> => {
    const next = playableChapters[playlistIndex + delta];
    if (next) await jumpToChapter(next);
  }, [jumpToChapter, playableChapters, playlistIndex]);

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
    if (library.selectedBookId) {
      setPlayerDismissedBookId(library.selectedBookId);
    }

    const audio = audioRef.current;
    if (audio && !audio.paused) {
      audio.pause();
    }

    void persistLoadedBookPlaybackPosition({ force: true, silent: true });
  }, [library.selectedBookId, persistLoadedBookPlaybackPosition]);

  const handlePlaybackError = useCallback(() => {
    setPlaybackError("Playback failed");
    const bookId = library.selectedBookId;
    const detail = library.selectedBookDetail;
    const audio = audioRef.current;
    if (!bookId || !detail?.playback_url || !audio) {
      return;
    }
    if (playbackRecoveryAttemptsRef.current.has(bookId)) {
      return;
    }

    const recoveryToken = Symbol("playback-recovery");
    playbackRecoveryAttemptsRef.current.set(bookId, recoveryToken);
    setPendingResume(
      bookId,
      Number(audio.currentTime || detail.playback_position_sec || 0),
      playbackCheckpointBookIdentity(detail),
    );
    setPlayerTitle("Reconnecting playback…");
    void loadBookAudio(bookId, detail, isPlaying, true).finally(() => {
      if (playbackRecoveryAttemptsRef.current.get(bookId) === recoveryToken) {
        playbackRecoveryAttemptsRef.current.delete(bookId);
      }
    });
  }, [isPlaying, library.selectedBookDetail, library.selectedBookId, loadBookAudio, setPendingResume]);

  const markPlaybackHealthy = useCallback(() => {
    const bookId = loadedBookIdRef.current ?? selectedBookIdLatestRef.current;
    if (bookId !== null) {
      playbackRecoveryAttemptsRef.current.delete(bookId);
    }
  }, []);

  useEffect(() => {
    if (previousApiClientRef.current === apiClient) {
      return;
    }
    previousApiClientRef.current = apiClient;
    loadSequenceRef.current += 1;
    playbackRecoveryAttemptsRef.current.clear();
    clearAudioSource();
  }, [apiClient, clearAudioSource]);

  useEffect(() => {
    const previousBookId = previousSelectedBookIdRef.current;
    const selectedBookId = library.selectedBookId;
    previousSelectedBookIdRef.current = selectedBookId;

    if (previousBookId === null || previousBookId === selectedBookId) {
      return;
    }

    void persistLoadedBookPlaybackPosition({
      bookId: previousBookId,
      force: true,
      silent: true,
    });
    if (loadedBookIdRef.current === previousBookId) {
      clearAudioSource();
    }
  }, [clearAudioSource, library.selectedBookId, persistLoadedBookPlaybackPosition]);

  useEffect(() => {
    if (library.selectedBookPlayable) {
      updatePlayerStatus();
      return;
    }

    clearAudioSource();
    updatePlayerStatus();
  }, [clearAudioSource, library.selectedBookPlayable, updatePlayerStatus]);

  useEffect(() => {
    updatePlayerStatus();
  }, [elapsedSec, isPlaying, totalSec, updatePlayerStatus]);

  const audioProps = useAudioElementBindings({
    audioRef,
    audioSrc,
    loadedBookId,
    volume,
    library,
    persistLoadedBookPlaybackPosition,
    markPlaybackSaved,
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
    setPlayerTitle,
    onPlaybackError: handlePlaybackError,
    onPlaybackHealthy: markPlaybackHealthy,
  });

  return {
    audioRef,
    audioProps,
    playerEligible,
    playerVisible,
    playing: isPlaying,
    title: playbackError || playerTitle,
    elapsedSec,
    durationSec,
    canSeek,
    volume,
    currentChapter,
    currentChapterLabel,
    chapterPlaylist,
    canGoPrevious,
    canGoNext,
    togglePlay,
    seekBy,
    seekTo,
    playRelative: chapterPlaylist ? playChapterRelative : playRelative,
    jumpToChapter,
    setPlayerVolume,
    closePlayer,
  };
}

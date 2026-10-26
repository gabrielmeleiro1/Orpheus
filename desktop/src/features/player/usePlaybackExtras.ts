import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

export const PLAYBACK_RATES = [0.75, 1, 1.25, 1.5, 1.75, 2] as const;
export const SLEEP_TIMER_MINUTES = [15, 30, 60] as const;

const RATE_STORAGE_KEY = "orpheus.playbackRate";

function readStoredRate(): number {
  try {
    const stored = Number(window.localStorage.getItem(RATE_STORAGE_KEY));
    return (PLAYBACK_RATES as readonly number[]).includes(stored) ? stored : 1;
  } catch {
    return 1;
  }
}

/** Narration speed, applied to every source the audio element loads and remembered between sessions. */
export function usePlaybackRate(audioRef: RefObject<HTMLAudioElement | null>, audioSrc: string | null | undefined) {
  const [playbackRate, setRate] = useState(readStoredRate);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    // A new src resets playbackRate to defaultPlaybackRate, so set both.
    audio.defaultPlaybackRate = playbackRate;
    audio.playbackRate = playbackRate;
  }, [audioRef, audioSrc, playbackRate]);

  const setPlaybackRate = useCallback((next: number) => {
    setRate(next);
    try {
      window.localStorage.setItem(RATE_STORAGE_KEY, String(next));
    } catch {
      // Storage can be unavailable; the rate still applies for this session.
    }
  }, []);

  return { playbackRate, setPlaybackRate };
}

export type SleepTimer =
  | { kind: "off" }
  // The minutes are listening time: while playback is paused the countdown
  // holds at pausedRemainingMs and endsAt is recomputed when playback resumes.
  | { kind: "minutes"; minutes: number; endsAt: number; pausedRemainingMs?: number }
  | { kind: "chapter"; chapterNumber: number | null };

export type SleepTimerChoice = "off" | "chapter" | number;

interface SleepPlaybackContext {
  bookKey: string;
  chapterAt: (positionSec: number) => { number: number | null; endSec: number | null };
}

/** The transport consults this before auto-advancing. Seeking re-arms the
 * chapter boundary, and selecting another book cancels a chapter timer. */
export function useSleepTimer(audioRef: RefObject<HTMLAudioElement | null>, context: SleepPlaybackContext) {
  const [sleepTimer, setTimer] = useState<SleepTimer>({ kind: "off" });
  const [now, setNow] = useState(() => Date.now());
  const timerRef = useRef<SleepTimer>(sleepTimer);
  const contextRef = useRef(context);
  useLayoutEffect(() => {
    contextRef.current = context;
  });
  const boundaryRef = useRef<{ bookKey: string; endSec: number | null } | null>(null);
  const seekingRef = useRef(false);
  const updateTimer = useCallback((timer: SleepTimer) => {
    timerRef.current = timer;
    setTimer(timer);
  }, []);
  const stopPlayback = useCallback(() => {
    updateTimer({ kind: "off" });
    boundaryRef.current = null;
    const audio = audioRef.current;
    if (audio && !audio.paused) audio.pause();
  }, [audioRef, updateTimer]);

  useEffect(() => {
    if (sleepTimer.kind !== "minutes" || sleepTimer.pausedRemainingMs !== undefined) return undefined;
    const tick = window.setInterval(() => setNow(Date.now()), 1_000);
    const done = window.setTimeout(stopPlayback, Math.max(0, sleepTimer.endsAt - Date.now()));
    return () => {
      window.clearInterval(tick);
      window.clearTimeout(done);
    };
  }, [sleepTimer, stopPlayback]);

  const minutesTimerActive = sleepTimer.kind === "minutes";
  useEffect(() => {
    const audio = audioRef.current;
    if (!minutesTimerActive || !audio) return undefined;
    const sync = () => {
      const timer = timerRef.current;
      if (timer.kind !== "minutes") return;
      if (audio.paused && timer.pausedRemainingMs === undefined) {
        updateTimer({ ...timer, pausedRemainingMs: Math.max(0, timer.endsAt - Date.now()) });
      } else if (!audio.paused && timer.pausedRemainingMs !== undefined) {
        const resumedAt = Date.now();
        setNow(resumedAt);
        updateTimer({ kind: "minutes", minutes: timer.minutes, endsAt: resumedAt + timer.pausedRemainingMs });
      }
    };
    audio.addEventListener("play", sync);
    audio.addEventListener("pause", sync);
    return () => {
      audio.removeEventListener("play", sync);
      audio.removeEventListener("pause", sync);
    };
  }, [audioRef, minutesTimerActive, updateTimer]);

  useEffect(() => {
    if (boundaryRef.current && boundaryRef.current.bookKey !== context.bookKey) {
      boundaryRef.current = null;
      updateTimer({ kind: "off" });
    }
    seekingRef.current = false;
  }, [context.bookKey, updateTimer]);

  const armChapter = useCallback(() => {
    const current = contextRef.current;
    const chapter = current.chapterAt(audioRef.current?.currentTime ?? 0);
    boundaryRef.current = { bookKey: current.bookKey, endSec: chapter.endSec };
    updateTimer({ kind: "chapter", chapterNumber: chapter.number });
  }, [audioRef, updateTimer]);

  const setSleepTimer = useCallback((choice: SleepTimerChoice) => {
    boundaryRef.current = null;
    if (choice === "off") {
      updateTimer({ kind: "off" });
    } else if (choice === "chapter") {
      armChapter();
    } else {
      const startedAt = Date.now();
      setNow(startedAt);
      const durationMs = choice * 60_000;
      updateTimer({
        kind: "minutes", minutes: choice, endsAt: startedAt + durationMs,
        ...(audioRef.current?.paused ? { pausedRemainingMs: durationMs } : {}),
      });
    }
  }, [armChapter, audioRef, updateTimer]);

  const onSeeking = useCallback(() => { seekingRef.current = true; }, []);
  const onSeeked = useCallback(() => {
    seekingRef.current = false;
    if (timerRef.current.kind === "chapter" && boundaryRef.current?.bookKey === contextRef.current.bookKey) armChapter();
  }, [armChapter]);
  const stopAtChapterEnd = useCallback((ended = false): boolean => {
    const boundary = boundaryRef.current;
    const audio = audioRef.current;
    if (timerRef.current.kind !== "chapter" || !boundary || !audio
      || boundary.bookKey !== contextRef.current.bookKey || seekingRef.current || audio.seeking) return false;
    if (!ended && audio.ended) return false;
    if (ended || (!audio.paused && boundary.endSec !== null && audio.currentTime >= boundary.endSec)) {
      stopPlayback();
      return true;
    }
    return false;
  }, [audioRef, stopPlayback]);

  const sleepRemainingSec = sleepTimer.kind === "minutes"
    ? Math.max(0, Math.ceil((sleepTimer.pausedRemainingMs ?? sleepTimer.endsAt - now) / 1_000))
    : null;

  return { sleepTimer, sleepRemainingSec, setSleepTimer, stopAtChapterEnd, onSeeking, onSeeked };
}

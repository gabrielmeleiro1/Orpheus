export function resolvePlayerDurationSec(input: {
  audioDurationSec: number;
  /** Sum of chapter durations for the packaged whole-book/document stream. Prefer this fallback. */
  bookDurationMs?: number | null;
  /** Secondary fallback when a book sum is unavailable (e.g. single known chapter). */
  chapterDurationMs?: number | null;
}): number {
  const audio = Number(input.audioDurationSec);
  if (Number.isFinite(audio) && audio > 0) {
    return Math.floor(audio);
  }

  const bookMs = Number(input.bookDurationMs ?? 0);
  if (Number.isFinite(bookMs) && bookMs > 0) {
    return Math.floor(bookMs / 1000);
  }

  const chapterMs = Number(input.chapterDurationMs ?? 0);
  if (Number.isFinite(chapterMs) && chapterMs > 0) {
    return Math.floor(chapterMs / 1000);
  }

  return 0;
}

/** Seek is ready once a book's audio is loaded into the element. */
export function resolvePlayerCanSeek(input: {
  audioSrc: string | null | undefined;
  loadedBookId: number | null;
}): boolean {
  return Boolean(input.audioSrc && input.loadedBookId != null);
}

type SeekableAudio = {
  src: string;
  currentTime: number;
  duration: number;
};

/**
 * Mutate audio.currentTime only when a book's audio is loaded.
 * Returns false (and leaves currentTime unchanged) when seek is not ready.
 */
export function seekAudioIfReady(input: {
  audio: SeekableAudio | null | undefined;
  loadedBookId: number | null;
  positionSeconds: number;
}): boolean {
  const audio = input.audio;
  if (!audio || !resolvePlayerCanSeek({ audioSrc: audio.src, loadedBookId: input.loadedBookId })) {
    return false;
  }

  const duration = Number.isFinite(audio.duration) ? audio.duration : input.positionSeconds;
  audio.currentTime = Math.max(0, Math.min(duration, input.positionSeconds));
  return true;
}

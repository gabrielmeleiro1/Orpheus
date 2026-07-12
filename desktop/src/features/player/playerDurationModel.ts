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

/** Seek is ready only when the selected book's audio source is the current loaded source. */
export function resolvePlayerCanSeek(input: {
  audioSrc: string | null | undefined;
  loadedBookId: number | null;
  selectedBookId: number | null;
}): boolean {
  return Boolean(
    input.audioSrc &&
      input.loadedBookId != null &&
      input.selectedBookId != null &&
      input.loadedBookId === input.selectedBookId,
  );
}

type SeekableAudio = {
  src: string;
  currentTime: number;
  duration: number;
};

/**
 * Mutate audio.currentTime only when the selected book matches the loaded source.
 * Returns false (and leaves currentTime unchanged) when seek is not ready.
 */
export function seekAudioIfReady(input: {
  audio: SeekableAudio | null | undefined;
  loadedBookId: number | null;
  selectedBookId: number | null;
  positionSeconds: number;
}): boolean {
  const audio = input.audio;
  if (
    !audio ||
    !resolvePlayerCanSeek({
      audioSrc: audio.src,
      loadedBookId: input.loadedBookId,
      selectedBookId: input.selectedBookId,
    })
  ) {
    return false;
  }

  const duration = Number.isFinite(audio.duration) ? audio.duration : input.positionSeconds;
  audio.currentTime = Math.max(0, Math.min(duration, input.positionSeconds));
  return true;
}

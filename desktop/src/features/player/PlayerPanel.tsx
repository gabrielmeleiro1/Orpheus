import {
  type ChangeEventHandler,
  type CSSProperties,
  type ReactEventHandler,
  type Ref,
} from "react";
import { useMemo } from "react";

import { formatClockWithHours } from "../../format/time";
import type { LibraryChapterDetail } from "../../types/api";
import { BookCover } from "../../ui/BookCover";
import { Icon } from "../../ui/icons";
import { ChapterSeekBar } from "./ChapterSeekBar";
import { chapterMarks } from "./chapterPositionModel";
import "./player.css";

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

interface PlayerPanelAudioProps {
  src?: string | null;
  preload?: "none" | "metadata" | "auto";
  onLoadedMetadata?: ReactEventHandler<HTMLAudioElement>;
  onDurationChange?: ReactEventHandler<HTMLAudioElement>;
  onPlay?: ReactEventHandler<HTMLAudioElement>;
  onPause?: ReactEventHandler<HTMLAudioElement>;
  onTimeUpdate?: ReactEventHandler<HTMLAudioElement>;
  onVolumeChange?: ReactEventHandler<HTMLAudioElement>;
  onEmptied?: ReactEventHandler<HTMLAudioElement>;
  onEnded?: ReactEventHandler<HTMLAudioElement>;
  onSeeking?: ReactEventHandler<HTMLAudioElement>;
  onSeeked?: ReactEventHandler<HTMLAudioElement>;
  onError?: ReactEventHandler<HTMLAudioElement>;
}

interface PlayerPanelProps {
  enabled: boolean;
  visible: boolean;
  playing: boolean;
  title: string;
  bookId?: number | null;
  bookSourceKey?: string | null;
  bookTitle?: string;
  bookAuthor?: string | null;
  chapterTitle?: string;
  /** Chapters of the loaded book; their starts are marked on the scrubber. */
  chapters?: LibraryChapterDetail[];
  elapsedSec: number;
  durationSec: number;
  /** Explicit seek readiness; do not infer from duration alone (seeded duration may precede audio load). */
  canSeek: boolean;
  volume: number;
  canGoPrevious?: boolean;
  canGoNext?: boolean;
  chapterPlaylist?: boolean;
  /** Previous/next step through the playing book's chapters rather than the library. */
  chapterTransport?: boolean;
  audioRef?: Ref<HTMLAudioElement>;
  audio?: PlayerPanelAudioProps;
  onTogglePlay: () => void;
  onSeekBy: (deltaSeconds: number) => void;
  onSeekTo: (positionSeconds: number) => void;
  onPrevious: () => void;
  onNext: () => void;
  onVolumeChange: (volumePercent: number) => void;
  onClose: () => void;
  /** Opens or closes the full Now Playing view; omit to keep the bar standalone. */
  onToggleNowPlaying?: () => void;
  nowPlayingOpen?: boolean;
  playbackRate?: number;
  /** Short countdown shown while a sleep timer is running. */
  sleepLabel?: string;
}

export function PlayerPanel({
  enabled,
  visible,
  playing,
  title,
  bookId,
  bookSourceKey,
  bookTitle,
  bookAuthor,
  chapterTitle,
  chapters,
  elapsedSec,
  durationSec,
  canSeek,
  volume,
  canGoPrevious = false,
  canGoNext = false,
  chapterPlaylist = false,
  chapterTransport = chapterPlaylist,
  audioRef,
  audio,
  onTogglePlay,
  onSeekBy,
  onSeekTo,
  onPrevious,
  onNext,
  onVolumeChange,
  onClose,
  onToggleNowPlaying,
  nowPlayingOpen = false,
  playbackRate = 1,
  sleepLabel,
}: PlayerPanelProps) {
  const normalizedDurationSec = Number.isFinite(durationSec) && durationSec > 0 ? durationSec : 0;
  const seekMax = Math.max(normalizedDurationSec, 1);
  const normalizedElapsedSec = clamp(
    Number.isFinite(elapsedSec) ? elapsedSec : 0,
    0,
    seekMax,
  );
  const normalizedVolume = clamp(
    Math.round(Number.isFinite(volume) ? volume : 0),
    0,
    100,
  );

  // A chapter playlist scrubs one chapter at a time, so only whole-book audio gets marks.
  const marks = useMemo(
    () => (chapterPlaylist || !chapters ? [] : chapterMarks(chapters, normalizedDurationSec)),
    [chapterPlaylist, chapters, normalizedDurationSec],
  );

  const seekDisabled = !enabled || !canSeek;
  const volumeDisabled = !enabled;
  const prevDisabled = !enabled || !canGoPrevious;
  const nextDisabled = !enabled || !canGoNext;
  const playDisabled = !enabled;
  const resolvedStatusText = title.trim() || "Ready";
  const playbackNotice = /failed|could not|reconnecting/i.test(resolvedStatusText) ? resolvedStatusText : "";

  const handleSeekChange: ChangeEventHandler<HTMLInputElement> = (event) => {
    const nextPosition = Number.parseFloat(event.currentTarget.value);
    if (!Number.isFinite(nextPosition)) {
      return;
    }
    onSeekTo(clamp(nextPosition, 0, seekMax));
  };

  const handleVolumeChange: ChangeEventHandler<HTMLInputElement> = (event) => {
    const nextVolume = Number.parseInt(event.currentTarget.value, 10);
    if (!Number.isFinite(nextVolume)) {
      return;
    }
    onVolumeChange(clamp(nextVolume, 0, 100));
  };

  return (
    <footer className="player-bar" hidden={!visible} aria-hidden={!visible} aria-label="Audiobook player">
      <div className="player-main page-container">
        <div className="player-meta">
          {bookTitle && onToggleNowPlaying ? (
            <button
              type="button"
              className="player-open-now-playing"
              aria-expanded={nowPlayingOpen}
              aria-controls={nowPlayingOpen ? "now-playing" : undefined}
              aria-label={`${nowPlayingOpen ? "Close" : "Open"} now playing: ${bookTitle}`}
              onClick={onToggleNowPlaying}
            >
              <BookCover bookId={bookId} sourceKey={bookSourceKey} title={bookTitle} author={bookAuthor} size="md" className="player-cover" />
              <span className="player-meta-text">
                <span id="player-status" className="player-track-title">{bookTitle}</span>
                {chapterTitle ? <span className="player-track-chapter">{chapterTitle}</span> : null}
              </span>
            </button>
          ) : (
            <>
              {bookTitle ? <BookCover bookId={bookId} sourceKey={bookSourceKey} title={bookTitle} author={bookAuthor} size="md" className="player-cover" /> : null}
              <div className="player-meta-text">
                <span id="player-status" className="player-track-title">
                  {bookTitle || resolvedStatusText}
                </span>
                {bookTitle && chapterTitle ? (
                  <span className="player-track-chapter">{chapterTitle}</span>
                ) : null}
              </div>
            </>
          )}
          {playbackNotice ? <span className="player-notice">{playbackNotice}</span> : null}
        </div>

        <div className="player-center">
          <div className="player-controls">
            <button
              id="prev-btn"
              className="btn btn-ghost btn-icon"
              type="button"
              data-tip={chapterTransport ? "Previous chapter" : "Previous audiobook"}
              aria-label={chapterTransport ? "Previous chapter" : "Previous audiobook"}
              disabled={prevDisabled}
              onClick={onPrevious}
            >
              <svg className="icon" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                <path d="M6 5v14" />
                <path d="M18 6l-8 6 8 6z" />
              </svg>
            </button>
            <button
              id="seek-back-btn"
              className="btn btn-ghost btn-icon"
              type="button"
              data-tip="Back 10 seconds"
              aria-label="Seek back 10 seconds"
              disabled={seekDisabled}
              onClick={() => onSeekBy(-10)}
            >
              <svg className="icon" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                <path d="M11 8l-4 4 4 4" />
                <path d="M17 8l-4 4 4 4" />
              </svg>
            </button>
            <button
              id="play-btn"
              className="btn btn-primary btn-icon player-play"
              type="button"
              data-tip={playing ? "Pause" : "Play"}
              aria-label={playing ? "Pause audiobook" : "Play audiobook"}
              disabled={playDisabled}
              onClick={onTogglePlay}
            >
              {playing ? (
                <svg className="icon" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                  <path d="M9 5v14M15 5v14" />
                </svg>
              ) : (
                <svg className="icon" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                  <path d="M8 5l11 7-11 7z" />
                </svg>
              )}
            </button>
            <button
              id="seek-fwd-btn"
              className="btn btn-ghost btn-icon"
              type="button"
              data-tip="Forward 10 seconds"
              aria-label="Seek forward 10 seconds"
              disabled={seekDisabled}
              onClick={() => onSeekBy(10)}
            >
              <svg className="icon" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                <path d="M7 8l4 4-4 4" />
                <path d="M13 8l4 4-4 4" />
              </svg>
            </button>
            <button
              id="next-btn"
              className="btn btn-ghost btn-icon"
              type="button"
              data-tip={chapterTransport ? "Next chapter" : "Next audiobook"}
              aria-label={chapterTransport ? "Next chapter" : "Next audiobook"}
              disabled={nextDisabled}
              onClick={onNext}
            >
              <svg className="icon" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                <path d="M18 5v14" />
                <path d="M6 6l8 6-8 6z" />
              </svg>
            </button>
          </div>

          <div className="player-timeline">
            <span id="player-elapsed" className="player-time tabular-nums">
              {formatClockWithHours(normalizedElapsedSec)}
            </span>
            <ChapterSeekBar
              id="player-seek"
              className="player-seek"
              valueSec={normalizedElapsedSec}
              maxSec={seekMax}
              label="Seek position"
              valueText={`${formatClockWithHours(normalizedElapsedSec)} of ${formatClockWithHours(normalizedDurationSec)}`}
              disabled={seekDisabled}
              marks={marks}
              onChange={handleSeekChange}
              onStep={onSeekBy}
            />
            <span id="player-duration" className="player-time tabular-nums">
              {formatClockWithHours(normalizedDurationSec)}
            </span>
          </div>
        </div>

        <div className="player-trailing">
          {/* Now Playing shows speed and the sleep countdown itself while it is open. */}
          {sleepLabel && !nowPlayingOpen ? <span className="player-sleep-chip tabular-nums">{sleepLabel}</span> : null}
          {playbackRate !== 1 && !nowPlayingOpen ? <span className="player-rate-chip tabular-nums" data-tip="Playback speed">{playbackRate}×</span> : null}
          {onToggleNowPlaying && bookTitle ? (
            <button
              id="player-now-playing-btn"
              className={`btn btn-ghost btn-icon${nowPlayingOpen ? " is-active" : ""}`}
              type="button"
              aria-label={nowPlayingOpen ? "Close now playing" : "Open now playing"}
              // Both Now Playing toggles use one pattern: a disclosure of the view.
              aria-expanded={nowPlayingOpen}
              aria-controls={nowPlayingOpen ? "now-playing" : undefined}
              data-tip={nowPlayingOpen ? "Close now playing" : "Now playing: chapters, speed, sleep timer"}
              onClick={onToggleNowPlaying}
            >
              <svg className="icon" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                {nowPlayingOpen ? <path d="M6 9l6 6 6-6" /> : <path d="M6 15l6-6 6 6" />}
              </svg>
            </button>
          ) : null}
          <div className="player-volume-wrap">
            <span className="player-volume-label" aria-hidden="true">
              <svg className="icon" viewBox="0 0 24 24" focusable="false">
                <path d="M4 9v6h4l5 4V5L8 9H4z" />
                <path d="M16.5 8.5a5 5 0 0 1 0 7" />
              </svg>
            </span>
            <input
              id="player-volume"
              className="field-range player-volume"
              type="range"
              min={0}
              max={100}
              step={1}
              value={normalizedVolume}
              style={{ "--range-fill": `${normalizedVolume}%` } as CSSProperties}
              aria-label="Volume"
              aria-valuetext={`${normalizedVolume}%`}
              disabled={volumeDisabled}
              onChange={handleVolumeChange}
            />
          </div>
          <button
            id="player-close-btn"
            className="btn btn-ghost btn-icon player-close-btn"
            type="button"
            data-tip="Close the player"
            aria-label="Close player"
            onClick={onClose}
          >
            <Icon name="x" />
          </button>
        </div>
      </div>
      <span className="sr-only" role="status" aria-atomic="true">{[bookTitle || "Audiobook player", chapterTitle].filter(Boolean).join(". ")}</span>
      <span className="sr-only" role="alert">{playbackNotice}</span>

      <audio
        id="audio-player"
        ref={audioRef}
        style={{ display: "none" }}
        preload={audio?.preload ?? "metadata"}
        crossOrigin="anonymous"
        src={audio?.src ?? undefined}
        onLoadedMetadata={audio?.onLoadedMetadata}
        onDurationChange={audio?.onDurationChange}
        onPlay={audio?.onPlay}
        onPause={audio?.onPause}
        onTimeUpdate={audio?.onTimeUpdate}
        onVolumeChange={audio?.onVolumeChange}
        onEmptied={audio?.onEmptied}
        onEnded={audio?.onEnded}
        onSeeking={audio?.onSeeking}
        onSeeked={audio?.onSeeked}
        onError={audio?.onError}
      ></audio>
    </footer>
  );
}

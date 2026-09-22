import {
  type ChangeEventHandler,
  type ReactEventHandler,
  type Ref,
} from "react";
import { formatClock } from "../../format/time";
import { Icon } from "../../ui/icons";
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
  onError?: ReactEventHandler<HTMLAudioElement>;
}

interface PlayerPanelProps {
  enabled: boolean;
  visible: boolean;
  playing: boolean;
  title: string;
  bookTitle?: string;
  chapterTitle?: string;
  elapsedSec: number;
  durationSec: number;
  /** Explicit seek readiness; do not infer from duration alone (seeded duration may precede audio load). */
  canSeek: boolean;
  volume: number;
  canGoPrevious?: boolean;
  canGoNext?: boolean;
  chapterPlaylist?: boolean;
  className?: string;
  audioRef?: Ref<HTMLAudioElement>;
  audio?: PlayerPanelAudioProps;
  onTogglePlay: () => void;
  onSeekBy: (deltaSeconds: number) => void;
  onSeekTo: (positionSeconds: number) => void;
  onPrevious: () => void;
  onNext: () => void;
  onVolumeChange: (volumePercent: number) => void;
  onClose: () => void;
}

export function PlayerPanel({
  enabled,
  visible,
  playing,
  title,
  bookTitle,
  chapterTitle,
  elapsedSec,
  durationSec,
  canSeek,
  volume,
  canGoPrevious = false,
  canGoNext = false,
  chapterPlaylist = false,
  className,
  audioRef,
  audio,
  onTogglePlay,
  onSeekBy,
  onSeekTo,
  onPrevious,
  onNext,
  onVolumeChange,
  onClose,
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

  const seekDisabled = !enabled || !canSeek;
  const volumeDisabled = !enabled;
  const prevDisabled = !enabled || !canGoPrevious;
  const nextDisabled = !enabled || !canGoNext;
  const playDisabled = !enabled;
  const resolvedStatusText = title.trim() || "Ready";
  const playbackNotice = /failed|could not|reconnecting/i.test(resolvedStatusText) ? resolvedStatusText : "";
  const rootClassName = className ? `player-bar ${className}` : "player-bar";

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
    <footer className={rootClassName} hidden={!visible} aria-hidden={!visible} aria-label="Audiobook player">
      <div className="player-main">
        <div className="player-meta">
          <span id="player-status" className="player-track-title" title={bookTitle || resolvedStatusText}>
            {bookTitle || resolvedStatusText}
          </span>
          {bookTitle && chapterTitle ? (
            <span className="player-track-chapter" title={chapterTitle}>{chapterTitle}</span>
          ) : null}
          {playbackNotice ? <span className="player-notice">{playbackNotice}</span> : null}
        </div>

        <div className="player-center">
          <div className="player-controls">
            <button
              id="prev-btn"
              className="btn-player btn-icon"
              type="button"
              data-tip={chapterPlaylist ? "Previous chapter" : "Previous audiobook"}
              aria-label={chapterPlaylist ? "Previous chapter" : "Previous audiobook"}
              disabled={prevDisabled}
              onClick={onPrevious}
            >
              <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                <path d="M6 5v14" />
                <path d="M18 6l-8 6 8 6z" />
              </svg>
            </button>
            <button
              id="seek-back-btn"
              className="btn-player btn-icon"
              type="button"
              data-tip="Back 10 seconds"
              aria-label="Seek back 10 seconds"
              disabled={seekDisabled}
              onClick={() => onSeekBy(-10)}
            >
              <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                <path d="M11 8l-4 4 4 4" />
                <path d="M17 8l-4 4 4 4" />
              </svg>
            </button>
            <button
              id="play-btn"
              className="btn-player btn-icon btn-play"
              type="button"
              data-tip={playing ? "Pause" : "Play"}
              aria-label={playing ? "Pause audiobook" : "Play audiobook"}
              disabled={playDisabled}
              onClick={onTogglePlay}
            >
              {playing ? (
                <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                  <path d="M9 5v14M15 5v14" />
                </svg>
              ) : (
                <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                  <path d="M8 5l11 7-11 7z" />
                </svg>
              )}
            </button>
            <button
              id="seek-fwd-btn"
              className="btn-player btn-icon"
              type="button"
              data-tip="Forward 10 seconds"
              aria-label="Seek forward 10 seconds"
              disabled={seekDisabled}
              onClick={() => onSeekBy(10)}
            >
              <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                <path d="M7 8l4 4-4 4" />
                <path d="M13 8l4 4-4 4" />
              </svg>
            </button>
            <button
              id="next-btn"
              className="btn-player btn-icon"
              type="button"
              data-tip={chapterPlaylist ? "Next chapter" : "Next audiobook"}
              aria-label={chapterPlaylist ? "Next chapter" : "Next audiobook"}
              disabled={nextDisabled}
              onClick={onNext}
            >
              <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
                <path d="M18 5v14" />
                <path d="M6 6l8 6-8 6z" />
              </svg>
            </button>
          </div>

          <div className="player-timeline">
            <span id="player-elapsed" className="player-time tabular-nums">
              {formatClock(normalizedElapsedSec)}
            </span>
            <input
              id="player-seek"
              className="player-seek"
              type="range"
              min={0}
              max={seekMax}
              step={0.1}
              value={normalizedElapsedSec}
              aria-label="Seek position"
              aria-valuetext={`${formatClock(normalizedElapsedSec)} of ${formatClock(normalizedDurationSec)}`}
              disabled={seekDisabled}
              onChange={handleSeekChange}
            />
            <span id="player-duration" className="player-time tabular-nums">
              {formatClock(normalizedDurationSec)}
            </span>
          </div>
        </div>

        <div className="player-trailing">
          <div className="player-volume-wrap">
            <span className="player-volume-label" aria-hidden="true">
              <svg viewBox="0 0 24 24" focusable="false">
                <path d="M4 9v6h4l5 4V5L8 9H4z" />
                <path d="M16.5 8.5a5 5 0 0 1 0 7" />
              </svg>
            </span>
            <input
              id="player-volume"
              className="player-volume"
              type="range"
              min={0}
              max={100}
              step={1}
              value={normalizedVolume}
              aria-label="Volume"
              aria-valuetext={`${normalizedVolume}%`}
              disabled={volumeDisabled}
              onChange={handleVolumeChange}
            />
          </div>
          <button
            id="player-close-btn"
            className="btn-player btn-icon player-close-btn"
            type="button"
            data-tip="Close the player"
            aria-label="Close player"
            onClick={onClose}
          >
            <Icon name="x" size={14} />
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
        onError={audio?.onError}
      ></audio>
    </footer>
  );
}

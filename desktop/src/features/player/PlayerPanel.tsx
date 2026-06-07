import {
  type ChangeEventHandler,
  type ReactEventHandler,
  type Ref,
} from "react";
import "./player.css";

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function formatPlayerTime(seconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const minutes = Math.floor(totalSeconds / 60);
  const remainder = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
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
  statusText?: string;
  elapsedSec: number;
  durationSec: number;
  volume: number;
  canGoPrevious?: boolean;
  canGoNext?: boolean;
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
  statusText,
  elapsedSec,
  durationSec,
  volume,
  canGoPrevious = false,
  canGoNext = false,
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

  const seekDisabled = !enabled || normalizedDurationSec <= 0;
  const volumeDisabled = !enabled;
  const prevDisabled = !enabled || !canGoPrevious;
  const nextDisabled = !enabled || !canGoNext;
  const playDisabled = !enabled;
  const resolvedStatusText = statusText?.trim() || title.trim() || "player: ready";
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
    <footer className={rootClassName} hidden={!visible} aria-hidden={!visible}>
      <div className="player-main">
        <div className="player-controls">
          <button id="prev-btn" className="btn-player" type="button" title="previous" aria-label="Previous audiobook" disabled={prevDisabled} onClick={onPrevious}>
            ⏮
          </button>
          <button
            id="seek-back-btn"
            className="btn-player"
            type="button"
            title="-10s"
            aria-label="Seek back 10 seconds"
            disabled={seekDisabled}
            onClick={() => onSeekBy(-10)}
          >
            -10
          </button>
          <button
            id="play-btn"
            className="btn-player btn-play"
            type="button"
            title="play/pause"
            aria-label={playing ? "Pause audiobook" : "Play audiobook"}
            disabled={playDisabled}
            onClick={onTogglePlay}
          >
            {playing ? "⏸" : "▶"}
          </button>
          <button
            id="seek-fwd-btn"
            className="btn-player"
            type="button"
            title="+10s"
            aria-label="Seek forward 10 seconds"
            disabled={seekDisabled}
            onClick={() => onSeekBy(10)}
          >
            +10
          </button>
          <button id="next-btn" className="btn-player" type="button" title="next" aria-label="Next audiobook" disabled={nextDisabled} onClick={onNext}>
            ⏭
          </button>
        </div>

        <div className="player-track-info">
          <div className="player-status-row">
            <span id="player-status" className="player-track-title">
              {resolvedStatusText}
            </span>
            <button id="player-close-btn" className="btn-player player-close-btn" type="button" title="close player" aria-label="Close player" onClick={onClose}>
              ×
            </button>
          </div>
          <div className="player-timeline">
            <span id="player-elapsed" className="player-time">
              {formatPlayerTime(normalizedElapsedSec)}
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
              disabled={seekDisabled}
              onChange={handleSeekChange}
            />
            <span id="player-duration" className="player-time">
              {formatPlayerTime(normalizedDurationSec)}
            </span>
          </div>
        </div>

        <div className="player-volume-wrap">
          <span className="player-volume-label">vol:</span>
          <input
            id="player-volume"
            className="player-volume"
            type="range"
            min={0}
            max={100}
            step={1}
            value={normalizedVolume}
            aria-label="Volume"
            disabled={volumeDisabled}
            onChange={handleVolumeChange}
          />
        </div>
      </div>

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

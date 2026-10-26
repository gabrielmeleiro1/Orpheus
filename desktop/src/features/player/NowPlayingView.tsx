import { useEffect, useMemo, useRef } from "react";

import { formatClock, formatClockWithHours, formatDurationClock, formatRemainingClock } from "../../format/time";
import type { LibraryChapterDetail } from "../../types/api";
import { BookCover } from "../../ui/BookCover";
import { Icon } from "../../ui/icons";
import { TermSelect } from "../conversion/TermSelect";
import { ChapterSeekBar } from "./ChapterSeekBar";
import { PlayingBars } from "./PlayingBars";
import { chapterMarks } from "./chapterPositionModel";
import { PLAYBACK_RATES, SLEEP_TIMER_MINUTES, type SleepTimer, type SleepTimerChoice } from "./usePlaybackExtras";
import "./now-playing.css";

interface NowPlayingViewProps {
  open: boolean;
  bookId?: number | null;
  bookSourceKey?: string | null;
  bookTitle?: string;
  bookAuthor?: string | null;
  chapters: LibraryChapterDetail[];
  currentChapterNumber: number | null;
  playing: boolean;
  elapsedSec?: number;
  durationSec?: number;
  canSeek?: boolean;
  /** Chapters play as separate files, so the scrubber spans one chapter. */
  chapterPlaylist?: boolean;
  playbackRate: number;
  sleepTimer: SleepTimer;
  sleepRemainingSec: number | null;
  onJumpToChapter: (chapter: LibraryChapterDetail) => void;
  onSeekTo?: (positionSeconds: number) => void;
  /** Relative seeks from the live position, for the scrubber's arrow keys. */
  onSeekBy?: (deltaSeconds: number) => void;
  onPlaybackRateChange: (rate: number) => void;
  onSleepTimerChange: (choice: SleepTimerChoice) => void;
  onClose: () => void;
}

const RATE_OPTIONS = PLAYBACK_RATES.map((rate) => ({ value: String(rate), label: `${rate}×` }));
const SLEEP_OPTIONS = [
  { value: "off", label: "Off" },
  ...SLEEP_TIMER_MINUTES.map((minutes) => ({ value: String(minutes), label: `${minutes} min` })),
  { value: "chapter", label: "End of chapter" },
];

/** A running timer reads as its time left, in the one place the timer is shown. */
function sleepOptions(timer: SleepTimer, remainingSec: number | null) {
  if (timer.kind !== "minutes" || remainingSec === null) return SLEEP_OPTIONS;
  const running = String(timer.minutes);
  return SLEEP_OPTIONS.map((option) => option.value === running ? { ...option, label: `${formatClock(remainingSec)} left` } : option);
}

function sleepValue(timer: SleepTimer): string {
  return timer.kind === "minutes" ? String(timer.minutes) : timer.kind;
}

/** Announced once per change; the countdown in the menu button ticks silently. */
function sleepAnnouncement(timer: SleepTimer): string {
  if (timer.kind === "minutes") return `Pausing in ${timer.minutes} minutes`;
  if (timer.kind === "chapter") return "Pausing at the end of this chapter";
  return "";
}

/** Full listening view: big cover and book details on the left, the chapter list on the right. */
export function NowPlayingView({
  open,
  bookId,
  bookSourceKey,
  bookTitle,
  bookAuthor,
  chapters,
  currentChapterNumber,
  playing,
  elapsedSec = 0,
  durationSec = 0,
  canSeek = false,
  chapterPlaylist = false,
  playbackRate,
  sleepTimer,
  sleepRemainingSec,
  onJumpToChapter,
  onSeekTo,
  onSeekBy,
  onPlaybackRateChange,
  onSleepTimerChange,
  onClose,
}: NowPlayingViewProps) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const viewRef = useRef<HTMLElement | null>(null);
  const currentRowRef = useRef<HTMLButtonElement | null>(null);
  const currentIndex = chapters.findIndex((chapter) => chapter.number === currentChapterNumber);
  const current = currentIndex >= 0 ? chapters[currentIndex] : null;
  const totalSec = Number.isFinite(durationSec) && durationSec > 0 ? durationSec : 0;
  const seekMax = Math.max(totalSec, 1);
  const positionSec = Math.max(0, Math.min(seekMax, Number.isFinite(elapsedSec) ? elapsedSec : 0));
  const marks = useMemo(
    () => (chapterPlaylist ? [] : chapterMarks(chapters, totalSec)),
    [chapterPlaylist, chapters, totalSec],
  );

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const view = viewRef.current;
    closeRef.current?.focus();
    currentRowRef.current?.scrollIntoView?.({ block: "center" });
    return () => {
      // Closing removes the focused control. Return keyboard users to the
      // opener, but preserve focus if they already moved to the transport.
      if (opener?.isConnected && !opener.closest("[hidden], [inert]")
        && (document.activeElement === document.body || view?.contains(document.activeElement))) {
        opener.focus({ preventScroll: true });
      }
    };
  }, [open]);

  // The scrubber re-renders this view every playback second; a long book's
  // rows change only with the current chapter, play state or the book itself.
  const chapterRows = useMemo(() => (open ? chapters.map((chapter) => {
    const isCurrent = chapter.number === currentChapterNumber;
    return (
      <li key={chapter.id}>
        <button
          ref={isCurrent ? currentRowRef : undefined}
          type="button"
          className={`np-chapter${isCurrent ? " is-current" : ""}`}
          aria-current={isCurrent ? "true" : undefined}
          disabled={chapterPlaylist && !chapter.mp3_path}
          onClick={() => onJumpToChapter(chapter)}
        >
          <span className="np-chapter-index tabular-nums" aria-hidden="true">
            {isCurrent && playing ? <PlayingBars /> : String(chapter.number).padStart(2, "0")}
          </span>
          <span className="np-chapter-title">{chapter.title || `Chapter ${chapter.number}`}</span>
          <span className="np-chapter-duration tabular-nums">{formatDurationClock(chapter.duration_ms)}</span>
        </button>
      </li>
    );
  }) : null), [chapterPlaylist, chapters, currentChapterNumber, onJumpToChapter, open, playing]);

  if (!open) return null;

  return (
    <section
      ref={viewRef}
      id="now-playing"
      className="np-view"
      role="dialog"
      aria-modal="false"
      aria-label="Now playing"
      onKeyDown={(event) => {
        // An open speed/sleep menu claims Escape first.
        if (event.key === "Escape" && !event.defaultPrevented) {
          event.preventDefault();
          onClose();
        }
      }}
    >
      <header className="np-view-top">
        <span className="eyebrow np-view-eyebrow">{playing ? "Now playing" : "Paused"}</span>
        <button
          ref={closeRef}
          type="button"
          className="btn btn-ghost btn-sm btn-icon"
          aria-label="Close now playing"
          data-tip="Close (Esc)"
          data-tip-pos="left"
          onClick={onClose}
        >
          <Icon name="x" />
        </button>
      </header>

      <div className="np-view-body">
        <div className="np-view-book">
          <BookCover bookId={bookId} sourceKey={bookSourceKey} title={bookTitle} author={bookAuthor} size="tile" className="np-view-cover" />
          <div className="np-view-meta">
            <h2 className="np-view-title">{bookTitle || "Untitled"}</h2>
            {bookAuthor?.trim() ? <p className="np-view-author">{bookAuthor}</p> : null}
            {current ? (
              <p className="np-view-chapter">
                <span className="tabular-nums">Chapter {current.number} of {chapters.length}</span>
                <span aria-hidden="true"> · </span>
                <span>{current.title || "Untitled"}</span>
              </p>
            ) : null}
          </div>

          {onSeekTo ? (
            <div className="np-scrubber">
              <ChapterSeekBar
                className="np-seek"
                valueSec={positionSec}
                maxSec={seekMax}
                label={chapterPlaylist ? "Chapter position" : "Book position"}
                valueText={`${formatClockWithHours(positionSec)} of ${formatClockWithHours(totalSec)}`}
                disabled={!canSeek}
                marks={marks}
                onChange={(event) => {
                  const next = Number.parseFloat(event.currentTarget.value);
                  if (Number.isFinite(next)) onSeekTo(Math.max(0, Math.min(seekMax, next)));
                }}
                onStep={onSeekBy}
              />
              <div className="np-scrubber-times tabular-nums">
                <span>{formatClockWithHours(positionSec)}</span>
                <span>-{formatRemainingClock(totalSec, positionSec)}</span>
              </div>
            </div>
          ) : null}

          <div className="np-view-controls">
            <div className="np-control">
              <span className="field-label np-control-label" aria-hidden="true">Speed</span>
              <TermSelect
                id="np-speed"
                className="control-sm"
                label="Playback speed"
                value={String(playbackRate)}
                options={RATE_OPTIONS}
                placeholder={`${playbackRate}×`}
                onChange={(value) => onPlaybackRateChange(Number(value))}
              />
            </div>
            <div className="np-control">
              <span className="field-label np-control-label" aria-hidden="true">Sleep timer</span>
              <TermSelect
                id="np-sleep"
                className="control-sm tabular-nums"
                label="Sleep timer"
                value={sleepValue(sleepTimer)}
                options={sleepOptions(sleepTimer, sleepRemainingSec)}
                placeholder="Off"
                onChange={(value) => onSleepTimerChange(value === "off" || value === "chapter" ? value : Number(value))}
              />
            </div>
            <span className="sr-only" role="status">{sleepAnnouncement(sleepTimer)}</span>
          </div>
        </div>

        <div className="np-view-chapters">
          <h3 className="np-chapters-header">Chapters</h3>
          <ol className="np-chapter-list">
            {chapterRows}
          </ol>
        </div>
      </div>
    </section>
  );
}

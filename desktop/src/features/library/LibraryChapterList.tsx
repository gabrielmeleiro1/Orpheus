import { memo } from "react";

import type { LibraryChapterDetail } from "../../types/api";
import { formatDurationClock } from "../../format/time";
import { Icon } from "../../ui/icons";
import { chapterPlaybackStatusLabel } from "../player/chapterPlaybackLabel";
import { PlayingBars } from "../player/PlayingBars";

interface ChapterListProps {
  chapters: LibraryChapterDetail[];
  currentChapterNumber?: number | null;
  playing: boolean;
  detailTransitionPending: boolean;
  cleaningPreviewLoading: boolean;
  chapterPlaylist?: boolean;
  playable?: boolean;
  onJumpToChapter?: (chapter: LibraryChapterDetail) => void;
  onLoadCleaningPreview: (chapterNumber: number) => Promise<void>;
}

// Metadata typing does not touch this section. Playback transitions update the
// old/current rows; unchanged rows retain their controls and DOM state.
const ChapterRow = memo(function ChapterRow({ chapter, isCurrent, playing,
  detailTransitionPending, cleaningPreviewLoading, chapterPlaylist, playable = true, onJumpToChapter, onLoadCleaningPreview,
}: Omit<ChapterListProps, "chapters" | "currentChapterNumber"> & {
  chapter: LibraryChapterDetail;
  isCurrent: boolean;
}) {
  const playbackLabel = chapterPlaybackStatusLabel({ isCurrent, playing });
  const title = chapter.title || `Chapter ${chapter.number}`;
  return (
    <div className={`chapter-row${isCurrent ? " is-current" : ""}${isCurrent && playing ? " is-playing" : ""}`}>
      <button type="button" className="chapter-main" aria-current={isCurrent ? "true" : undefined}
        aria-label={playbackLabel ? `${title} — ${playbackLabel.toLowerCase()}` : undefined}
        onClick={() => onJumpToChapter?.(chapter)}
        disabled={detailTransitionPending || !playable || (chapterPlaylist && !chapter.mp3_path) || !onJumpToChapter}>
        {/* One slot: the number, the play glyph on hover, or bars while this chapter plays. */}
        <span className="chapter-index" aria-hidden="true">
          <span className="chapter-number tabular-nums">{String(chapter.number).padStart(2, "0")}</span>
          <span className="chapter-play-affordance"><Icon name="play" /></span>
          {isCurrent && playing ? <PlayingBars className="chapter-playing" /> : null}
        </span>
        <span className="chapter-title">{title}</span>
        <span className="chapter-duration tabular-nums">{formatDurationClock(chapter.duration_ms)}</span>
      </button>
      <button className="btn btn-ghost btn-sm btn-icon chapter-cleaned-btn" type="button"
        disabled={detailTransitionPending || cleaningPreviewLoading}
        aria-label={`Original and cleaned text: ${title}`}
        data-tip="Original and cleaned text"
        data-tip-pos="left"
        onClick={() => void onLoadCleaningPreview(chapter.number)}>
        <Icon name="document" />
      </button>
    </div>
  );
});

export const LibraryChapterList = memo(function LibraryChapterList({ chapters, currentChapterNumber,
  playing, detailTransitionPending, cleaningPreviewLoading, chapterPlaylist, playable, onJumpToChapter, onLoadCleaningPreview,
}: ChapterListProps) {
  return (
    <section className="chapter-section" aria-labelledby="chapter-list-title">
      <h3 id="chapter-list-title" className="album-section-title">Chapters</h3>
      <div id="chapter-list" className="chapter-list">
        {chapters.length === 0 ? <p className="hint chapter-empty">No chapters yet</p> : chapters.map((chapter) => (
          <ChapterRow key={chapter.id} chapter={chapter} isCurrent={chapter.number === currentChapterNumber}
            playing={playing && chapter.number === currentChapterNumber}
            detailTransitionPending={detailTransitionPending} cleaningPreviewLoading={cleaningPreviewLoading}
            chapterPlaylist={chapterPlaylist} playable={playable}
            onJumpToChapter={onJumpToChapter} onLoadCleaningPreview={onLoadCleaningPreview} />
        ))}
      </div>
    </section>
  );
});

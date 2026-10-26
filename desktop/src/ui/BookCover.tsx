import { memo, useState, type CSSProperties } from "react";

import { useCoverArt } from "./coverArt";
import "./book-cover.css";

type BookCoverSize = "sm" | "md" | "xl" | "tile";

interface BookCoverProps {
  title?: string | null;
  author?: string | null;
  /** Library book whose real cover art to show; without it the generated cover is used. */
  bookId?: number | null;
  /** Changes when the book's source changes, so a replaced source refetches its cover. */
  sourceKey?: string | null;
  size?: BookCoverSize;
  className?: string;
}

// FNV-1a keeps a book's cover stable across sessions without storing anything.
function hashSeed(value: string): number {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return hash >>> 0;
}

function coverSeed(title?: string | null, author?: string | null): number {
  return hashSeed(`${title?.trim() || "Untitled"}\u0000${author ?? ""}`.toLowerCase());
}

function waveHeights(seed: number, count: number): number[] {
  let state = seed || 1;
  return Array.from({ length: count }, () => {
    state = Math.imul(state ^ (state >>> 15), 0x2c1b3c6d) >>> 0;
    return 0.15 + (state % 1000) / 1180;
  });
}

const BAR_COUNT = { compact: 7, large: 13 } as const;

/**
 * The book's own waveform, centred on the cover. The title and author sit next
 * to every cover already, so the generated cover carries no text of its own.
 */
function CoverWave({ seed, bars }: { seed: number; bars: number }) {
  const heights = waveHeights(seed, bars);
  const pitch = 24 / bars;
  return (
    <svg className="book-cover-bars" viewBox="0 0 28 28" focusable="false" aria-hidden="true">
      {heights.map((height, index) => {
        const barHeight = Math.max(3, Math.round(height * 18));
        return <rect key={index} x={2 + index * pitch + (pitch - pitch * 0.48) / 2} y={14 - barHeight / 2} width={pitch * 0.48} height={barHeight} rx={pitch * 0.24} />;
      })}
    </svg>
  );
}

/**
 * Book cover: the source's own cover art when it has one, otherwise a generated graphite cover with the book's waveform.
 * Memoized: shelves and the sidebar render one per book, and their parents re-render on every playback tick and progress event.
 */
export const BookCover = memo(function BookCover({ title, author, bookId, sourceKey, size = "md", className }: BookCoverProps) {
  const seed = coverSeed(title, author);
  const artUrl = useCoverArt(bookId, sourceKey ?? "");
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const showArt = Boolean(artUrl && artUrl !== failedUrl);
  // Monochrome covers: each book gets its own shade of graphite; the waveform does the rest.
  const style = { "--cover-l": `${10 + (seed % 7) * 2.8}%` } as CSSProperties;
  const compact = size === "sm" || size === "md";
  return (
    <span
      className={`book-cover is-${size}${showArt ? " has-art" : ""}${className ? ` ${className}` : ""}`}
      style={style}
      aria-hidden="true"
    >
      {showArt ? (
        <img className="book-cover-art" src={artUrl ?? undefined} alt="" draggable={false} onError={() => setFailedUrl(artUrl)} />
      ) : (
        <CoverWave seed={seed} bars={compact ? BAR_COUNT.compact : BAR_COUNT.large} />
      )}
    </span>
  );
});

import "./ascii.css";

const FILLED = "▰";
const EMPTY = "▱";
const SEGMENT_COUNT = 24;

interface GlyphProgressProps {
  pct: number;
  className?: string;
}

export function GlyphProgress({ pct, className }: GlyphProgressProps) {
  const bounded = Math.min(100, Math.max(0, pct));
  const filled = Math.round((bounded / 100) * SEGMENT_COUNT);

  return (
    <span className={className ? `glyph-progress ${className}` : "glyph-progress"} aria-hidden="true">
      <span className="glyph-progress-filled">{FILLED.repeat(filled)}</span>
      <span className="glyph-progress-empty">{EMPTY.repeat(SEGMENT_COUNT - filled)}</span>
    </span>
  );
}

import "./ascii.css";

const BLOCKS = [" ", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"];

/** Renders column heights (0–1) as a block-glyph waveform `rows` characters tall. */
function waveformRows(heights: readonly number[], rows: number): string[] {
  const levels = rows * 8;
  const lines: string[] = [];
  for (let row = rows - 1; row >= 0; row -= 1) {
    lines.push(heights.map((height) => {
      const filled = Math.round(Math.max(0, Math.min(1, height)) * levels) - row * 8;
      return BLOCKS[Math.max(0, Math.min(8, filled))];
    }).join(""));
  }
  return lines;
}

// Fixed shapes so empty states never shift between renders.
const SPEECH = Array.from({ length: 36 }, (_, index) =>
  0.12 + 0.88 * Math.abs(Math.sin(index * 0.55) * Math.sin(index * 0.17 + 0.6)));
const SILENCE = Array.from({ length: 36 }, (_, index) => 0.04 + 0.05 * Math.abs(Math.sin(index * 0.9)));

const ART = {
  wave: waveformRows(SPEECH, 3),
  idle: waveformRows(SILENCE, 1),
} as const;

type AsciiArtName = keyof typeof ART;

/** A small block-glyph illustration for empty states. */
export function AsciiArt({ name, className }: { name: AsciiArtName; className?: string }) {
  return (
    <pre className={`ascii-art${className ? ` ${className}` : ""}`} aria-hidden="true">
      {ART[name].join("\n")}
    </pre>
  );
}

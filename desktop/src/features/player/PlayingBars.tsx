import "./player.css";

/** Three bars that bounce while audio plays; decorative, the caller names the state. */
export function PlayingBars({ className }: { className?: string }) {
  return (
    <span className={`playing-bars${className ? ` ${className}` : ""}`} aria-hidden="true">
      <span />
      <span />
      <span />
    </span>
  );
}

import type { CSSProperties, ChangeEventHandler, KeyboardEvent } from "react";

import type { ChapterMark } from "./chapterPositionModel";

interface ChapterSeekBarProps {
  id?: string;
  className?: string;
  valueSec: number;
  maxSec: number;
  disabled: boolean;
  label: string;
  valueText: string;
  marks: ChapterMark[];
  onChange: ChangeEventHandler<HTMLInputElement>;
  /** Keyboard steps, in seconds from the live position (the rendered value lags playback). */
  onStep?: (deltaSec: number) => void;
}

const KEY_STEP_SEC: Record<string, number> = { ArrowRight: 5, ArrowUp: 5, ArrowLeft: -5, ArrowDown: -5, PageUp: 60, PageDown: -60 };

/** Book scrubber with a notch where each chapter begins. */
export function ChapterSeekBar({ id, className, valueSec, maxSec, disabled, label, valueText, marks, onChange, onStep }: ChapterSeekBarProps) {
  const onKeyDown = onStep ? (event: KeyboardEvent<HTMLInputElement>) => {
    const step = KEY_STEP_SEC[event.key];
    if (step === undefined) return;
    event.preventDefault();
    onStep(step);
  } : undefined;
  return (
    <span className={`seek-track${marks.length > 0 ? " has-marks" : ""}`}>
      <input
        id={id}
        className={`field-range ${className ?? ""}`.trim()}
        type="range"
        min={0}
        max={maxSec}
        step={0.1}
        value={valueSec}
        style={{ "--range-fill": `${(valueSec / maxSec) * 100}%` } as CSSProperties}
        aria-label={label}
        aria-valuetext={valueText}
        disabled={disabled}
        onChange={onChange}
        onKeyDown={onKeyDown}
      />
      {marks.length > 0 ? (
        <span className="seek-marks" aria-hidden="true">
          {marks.map((mark) => (
            <span key={mark.number} className="seek-mark" style={{ left: `${mark.pct}%` }} />
          ))}
        </span>
      ) : null}
    </span>
  );
}

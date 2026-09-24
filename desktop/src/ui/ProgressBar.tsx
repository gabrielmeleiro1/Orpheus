import "./progress-bar.css";

type ProgressBarTone = "neutral" | "active" | "done" | "danger";

interface ProgressBarProps {
  pct: number | undefined;
  label: string;
  tone?: ProgressBarTone;
  /** Bar height variant. "hairline" for dense rows, "regular" elsewhere, "lg" for hero bars. */
  size?: "hairline" | "regular" | "lg";
  className?: string;
}

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round(value)));
}

export function ProgressBar({ pct, label, tone = "neutral", size = "regular", className }: ProgressBarProps) {
  const bounded = pct === undefined ? undefined : clampPercent(pct);
  const classes = ["pbar", `pbar-${tone}`, `pbar-${size}`];
  if (bounded === undefined) {
    classes.push("pbar-indeterminate");
  }
  if (className) {
    classes.push(className);
  }

  return (
    <div
      className={classes.join(" ")}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={bounded}
    >
      <span className="pbar-fill" style={{ width: bounded === undefined ? "35%" : `${bounded}%` }} />
    </div>
  );
}

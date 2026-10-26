import { type CSSProperties, type RefObject, useLayoutEffect, useRef, useState } from "react";

import "./progress-bar.css";

type ProgressBarTone = "neutral" | "active" | "done" | "danger";

interface ProgressBarProps {
  pct: number | undefined;
  label: string;
  tone?: ProgressBarTone;
  /** Bar height variant. "hairline" for dense rows, "regular" elsewhere, "lg" for hero bars. */
  size?: "hairline" | "regular" | "lg";
  /** "ascii" draws the bar as text cells (█ filled, ░ empty) for terminal-style views. */
  variant?: "bar" | "ascii";
  /**
   * Cell count for the ascii variant. "fit" fills the available width exactly,
   * so bars stacked in one column always share both edges.
   */
  cells?: number | "fit";
}

/** Cells drawn before a "fit" bar has been measured (and in layout-less tests). */
const FIT_FALLBACK_CELLS = 32;
/** Geist Mono advances 0.6em; the extra quarter keeps a hairline of air between cells. */
const CELL_WIDTH_EM = 0.6 * 1.25;

function clampPercent(value: number): number {
  if (!Number.isFinite(value)) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round(value)));
}

function splitCells(pct: number | undefined, cells: number): { filled: number; empty: number } {
  const count = Math.max(1, Math.round(cells));
  const filled = pct === undefined ? 0 : Math.round((clampPercent(pct) / 100) * count);
  return { filled, empty: count - filled };
}

function useFitCells(enabled: boolean, ref: RefObject<HTMLDivElement>): number {
  const [cells, setCells] = useState(FIT_FALLBACK_CELLS);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!enabled || !element || typeof ResizeObserver === "undefined") return undefined;
    // Live progress text resizes the bar slightly on most events; only a new
    // cell count is worth a render (an equal state update still costs one).
    let measured: number | null = null;
    const measure = () => {
      const fontSize = parseFloat(getComputedStyle(element).fontSize) || 12;
      const width = element.clientWidth;
      if (width <= 0) return;
      const next = Math.max(8, Math.floor(width / (fontSize * CELL_WIDTH_EM)));
      if (next === measured) return;
      measured = next;
      setCells(next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [enabled, ref]);
  return cells;
}

export function ProgressBar({ pct, label, tone = "neutral", size = "regular", variant = "bar", cells = 24 }: ProgressBarProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const fit = variant === "ascii" && cells === "fit";
  const fitCells = useFitCells(fit, rootRef);
  const bounded = pct === undefined ? undefined : clampPercent(pct);
  const classes = ["pbar", `pbar-${tone}`, `pbar-${size}`];
  if (variant === "ascii") {
    classes.push("pbar-ascii");
  }
  if (fit) {
    classes.push("pbar-fit");
  }
  if (bounded === undefined) {
    classes.push("pbar-indeterminate");
  }
  const cellCount = fit ? fitCells : typeof cells === "number" ? cells : FIT_FALLBACK_CELLS;
  const { filled, empty } = splitCells(bounded, cellCount);

  return (
    <div
      ref={rootRef}
      className={classes.join(" ")}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={bounded}
    >
      {variant === "ascii" ? (
        fit ? (
          // One grid track per cell: the bar spans its box edge to edge.
          <span className="pbar-cells" aria-hidden="true" style={{ "--pbar-cells": filled + empty } as CSSProperties}>
            <span className="pbar-cells-filled">{Array.from({ length: filled }, (_, index) => <span key={index}>█</span>)}</span>
            <span className="pbar-cells-empty">{Array.from({ length: empty }, (_, index) => <span key={index}>░</span>)}</span>
          </span>
        ) : (
          <span className="pbar-cells" aria-hidden="true">
            <span className="pbar-cells-filled">{"█".repeat(filled)}</span>
            <span className="pbar-cells-empty">{"░".repeat(empty)}</span>
          </span>
        )
      ) : (
        <span className="pbar-fill" style={{ width: bounded === undefined ? "35%" : `${bounded}%` }} />
      )}
    </div>
  );
}

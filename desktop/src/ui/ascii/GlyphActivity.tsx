import { useEffect, useMemo, useState } from "react";

import { usePrefersReducedMotion } from "./usePrefersReducedMotion";
import "./ascii.css";

const LEVELS = ["▁", "▂", "▃", "▄", "▅", "▆", "▇"];
const GLYPH_COUNT = 40;
const TICK_INTERVAL_MS = 120;

interface GlyphActivityProps {
  active: boolean;
  className?: string;
}

function levelAt(position: number, phase: number, active: boolean): string {
  if (!active) {
    const idle = 0.16 + Math.sin(position * 0.4) * 0.1;
    return LEVELS[Math.max(0, Math.round(idle * (LEVELS.length - 1)))];
  }
  const t = phase + position;
  const wave =
    Math.sin(t * 0.55) * 0.35 +
    Math.sin(t * 0.21 + 1.7) * 0.3 +
    Math.sin(t * 0.083 + 0.5) * 0.25;
  const value = 0.5 + wave * 0.5;
  const index = Math.min(LEVELS.length - 1, Math.max(0, Math.round(value * (LEVELS.length - 1))));
  return LEVELS[index];
}

export function GlyphActivity({ active, className }: GlyphActivityProps) {
  const reducedMotion = usePrefersReducedMotion();
  const [phase, setPhase] = useState(0);
  const animating = active && !reducedMotion;

  useEffect(() => {
    if (!animating) {
      return undefined;
    }
    const id = window.setInterval(() => {
      setPhase((current) => current + 1);
    }, TICK_INTERVAL_MS);
    return () => {
      window.clearInterval(id);
    };
  }, [animating]);

  const glyphs = useMemo(() => {
    let out = "";
    for (let position = 0; position < GLYPH_COUNT; position += 1) {
      out += levelAt(position, phase, animating);
    }
    return out;
  }, [phase, animating]);

  const stateClass = active ? "glyph-activity is-active" : "glyph-activity";
  return (
    <span className={className ? `${stateClass} ${className}` : stateClass} aria-hidden="true">
      {glyphs}
    </span>
  );
}

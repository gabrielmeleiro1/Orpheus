import { useEffect, useState } from "react";

import { usePrefersReducedMotion } from "./usePrefersReducedMotion";
import "./ascii.css";

const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
const FRAME_INTERVAL_MS = 80;

interface BrailleSpinnerProps {
  active?: boolean;
  className?: string;
}

export function BrailleSpinner({ active = true, className }: BrailleSpinnerProps) {
  const reducedMotion = usePrefersReducedMotion();
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    if (!active || reducedMotion) {
      return undefined;
    }
    const id = window.setInterval(() => {
      setFrame((current) => (current + 1) % FRAMES.length);
    }, FRAME_INTERVAL_MS);
    return () => {
      window.clearInterval(id);
    };
  }, [active, reducedMotion]);

  return (
    <span className={className ? `braille-spinner ${className}` : "braille-spinner"} aria-hidden="true">
      {FRAMES[active && !reducedMotion ? frame : 0]}
    </span>
  );
}

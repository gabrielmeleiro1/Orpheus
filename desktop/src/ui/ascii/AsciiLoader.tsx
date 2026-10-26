import { useEffect, useRef, useState } from "react";

import { usePrefersReducedMotion } from "./usePrefersReducedMotion";
import "./ascii.css";

type AsciiLoaderVariant = "scan" | "ring";

const RING_FRAMES = ["◜", "◠", "◝", "◞", "◡", "◟"];
const FRAME_INTERVAL_MS: Record<AsciiLoaderVariant, number> = { scan: 70, ring: 120 };
// Swapping characters is not movement, so reduced motion slows the loader
// instead of freezing it; a frozen loader reads as a hung app.
const REDUCED_MOTION_INTERVAL_MS = 420;

/** A bracketed track with a bright head sweeping back and forth. */
export function scanFrame(tick: number, width: number): string {
  const span = Math.max(1, width - 1);
  const cycle = span * 2;
  const position = Math.abs((((tick % cycle) + cycle) % cycle) - span);
  let track = "";
  for (let index = 0; index < width; index += 1) {
    const distance = Math.abs(index - position);
    track += distance === 0 ? "█" : distance === 1 ? "▓" : distance === 2 ? "▒" : distance === 3 ? "░" : "·";
  }
  return `[${track}]`;
}

function frameFor(variant: AsciiLoaderVariant, tick: number, width: number): string {
  return variant === "ring" ? RING_FRAMES[tick % RING_FRAMES.length] : scanFrame(tick, width);
}

interface AsciiLoaderProps {
  active?: boolean;
  variant?: AsciiLoaderVariant;
  /** Track cells for the scan variant. */
  width?: number;
  className?: string;
}

export function AsciiLoader({ active = true, variant = "scan", width = 5, className }: AsciiLoaderProps) {
  const reducedMotion = usePrefersReducedMotion();
  const loaderRef = useRef<HTMLSpanElement>(null);
  const [tick, setTick] = useState(0);
  const interval = reducedMotion ? REDUCED_MOTION_INTERVAL_MS : FRAME_INTERVAL_MS[variant];

  useEffect(() => {
    if (!active) {
      return undefined;
    }
    const loader = loaderRef.current;
    if (!loader) return undefined;

    let timer: number | undefined;
    let intersecting = true;
    const stop = () => {
      window.clearInterval(timer);
      timer = undefined;
    };
    // Only animate while the loader can actually be seen.
    const update = () => {
      const visible = !document.hidden && intersecting && loader.getClientRects().length > 0
        && getComputedStyle(loader).visibility === "visible";
      if (!visible) {
        stop();
      } else if (timer === undefined) {
        timer = window.setInterval(() => setTick((current) => current + 1), interval);
      }
    };
    const intersection = new IntersectionObserver(([entry]) => {
      intersecting = entry.isIntersecting;
      update();
    });
    intersection.observe(loader);
    const mutations = new MutationObserver(update);
    for (let ancestor: HTMLElement | null = loader; ancestor; ancestor = ancestor.parentElement) {
      mutations.observe(ancestor, { attributes: true, attributeFilter: ["hidden", "class", "style"] });
    }
    document.addEventListener("visibilitychange", update);
    window.addEventListener("resize", update);
    update();
    return () => {
      stop();
      intersection.disconnect();
      mutations.disconnect();
      document.removeEventListener("visibilitychange", update);
      window.removeEventListener("resize", update);
    };
  }, [active, interval]);

  const classes = ["ascii-loader", `ascii-loader-${variant}`, className].filter(Boolean).join(" ");
  return (
    <span ref={loaderRef} className={classes} aria-hidden="true">
      {frameFor(variant, active ? tick : 0, width)}
    </span>
  );
}

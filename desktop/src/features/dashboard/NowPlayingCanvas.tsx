import { useEffect, useRef, useState } from "react";

import type { AudioAnalyserHandle } from "../player/useAudioAnalyser";

/**
 * Now Playing visual: a lyre laid on its side.
 *
 * Six strings span the card. Each string is pinned at both ends and
 * vibrates as a standing wave driven by its own frequency band of the
 * live analyser signal. Sharp attacks pluck the string and release a
 * small mote of light that drifts upward. Idle, the strings settle to
 * a slow breathing shimmer.
 */

const FRAME_INTERVAL_MS = 1000 / 30;
const STRING_COUNT = 6;
const STRING_SEGMENTS = 64;
const MAX_MOTES = 14;

interface NowPlayingCanvasProps {
  visualizer: AudioAnalyserHandle;
  playing: boolean;
  active: boolean;
  className?: string;
}

interface StringState {
  /** Smoothed 0..1 excitation driving amplitude. */
  amp: number;
  /** Previous raw band value, for pluck detection. */
  prevBand: number;
  /** Standing-wave phase. */
  phase: number;
  /** Vibration mode (number of antinodes). */
  mode: number;
  /** Phase speed in radians per frame at full excitation. */
  speed: number;
}

interface Mote {
  x: number;
  y: number;
  vx: number;
  vy: number;
  life: number;
  size: number;
}

interface Palette {
  accent: string;
  accentMuted: string;
  faint: string;
}

function resolvePalette(canvas: HTMLCanvasElement): Palette {
  const styles = getComputedStyle(canvas);
  const read = (name: string, fallback: string) => styles.getPropertyValue(name).trim() || fallback;
  return {
    accent: read("--accent", "#c9a06c"),
    accentMuted: read("--accent-muted", "#b08d5e"),
    faint: read("--text-tertiary", "#5e5a56"),
  };
}

function createStrings(): StringState[] {
  return Array.from({ length: STRING_COUNT }, (_, index) => ({
    amp: 0,
    prevBand: 0,
    phase: Math.random() * Math.PI * 2,
    mode: 1 + (index % 3),
    speed: 0.16 + index * 0.028,
  }));
}

function resizeCanvas(canvas: HTMLCanvasElement): void {
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, Math.round(rect.width * ratio));
  const height = Math.max(1, Math.round(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function stringBaseY(index: number, height: number): number {
  return height * (0.16 + (index / (STRING_COUNT - 1)) * 0.68);
}

/** Log-spaced frequency band average, 0..1. */
function bandLevel(buffer: Uint8Array, index: number): number {
  const bins = buffer.length;
  const lo = Math.floor(bins * Math.pow(index / STRING_COUNT, 1.6));
  const hi = Math.max(lo + 1, Math.floor(bins * Math.pow((index + 1) / STRING_COUNT, 1.6)));
  let total = 0;
  for (let i = lo; i < hi && i < bins; i += 1) {
    total += buffer[i];
  }
  return total / ((hi - lo) * 255);
}

function drawScene(
  canvas: HTMLCanvasElement,
  strings: StringState[],
  motes: Mote[],
  palette: Palette,
  energy: number,
  rendering: "live" | "idle" | "static",
): void {
  const context = canvas.getContext("2d");
  if (!context) {
    return;
  }
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, canvas.width / ratio);
  const height = Math.max(1, canvas.height / ratio);

  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  context.lineCap = "round";
  context.lineJoin = "round";

  for (let s = 0; s < strings.length; s += 1) {
    const string = strings[s];
    const baseY = stringBaseY(s, height);
    const maxSwing = height * 0.075;
    const swing = maxSwing * string.amp;
    // Lower strings read as heavier: thicker core, slower shimmer.
    const weight = 1 + (s / (STRING_COUNT - 1)) * 0.9;

    const trace = (scale: number) => {
      context.beginPath();
      for (let i = 0; i <= STRING_SEGMENTS; i += 1) {
        const t = i / STRING_SEGMENTS;
        const x = t * width;
        const pin = Math.sin(Math.PI * t);
        const wave = Math.sin(Math.PI * string.mode * t + string.phase);
        const y = baseY + wave * pin * swing * scale;
        if (i === 0) {
          context.moveTo(x, y);
        } else {
          context.lineTo(x, y);
        }
      }
    };

    // Halo pass.
    context.globalAlpha = 0.05 + string.amp * 0.3;
    context.strokeStyle = palette.accentMuted;
    context.lineWidth = weight * 4.4;
    trace(0.92);
    context.stroke();

    // Core pass.
    context.globalAlpha = 0.28 + string.amp * 0.62;
    context.strokeStyle = string.amp > 0.04 ? palette.accent : palette.faint;
    context.lineWidth = weight;
    trace(1);
    context.stroke();
  }

  // Motes: released note-lights.
  for (const mote of motes) {
    const fade = Math.max(0, mote.life);
    context.globalAlpha = fade * 0.85;
    context.fillStyle = palette.accent;
    context.beginPath();
    context.arc(mote.x, mote.y, mote.size * (0.6 + fade * 0.4), 0, Math.PI * 2);
    context.fill();
  }

  context.globalAlpha = 1;
  canvas.dataset.waveformEnergy = energy.toFixed(4);
  if (canvas.dataset.rendering !== rendering) {
    canvas.dataset.rendering = rendering;
  }
}

export function NowPlayingCanvas({ visualizer, playing, active, className }: NowPlayingCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const frameRef = useRef<number | null>(null);
  const lastFrameAtRef = useRef(0);
  const frameCountRef = useRef(0);
  const stringsRef = useRef<StringState[]>(createStrings());
  const motesRef = useRef<Mote[]>([]);
  const freqBufferRef = useRef<Uint8Array<ArrayBuffer> | null>(null);
  const paletteRef = useRef<Palette>({ accent: "#c9a06c", accentMuted: "#b08d5e", faint: "#5e5a56" });
  const idlePhaseRef = useRef(0);
  const [reducedMotion, setReducedMotion] = useState(() =>
    window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  const [documentVisible, setDocumentVisible] = useState(() => document.visibilityState === "visible");

  const rootState = playing ? "playback-waveform is-playing" : "playback-waveform is-idle";
  const statusClass = `is-${visualizer.status}`;
  const rootClassName = className ? `${rootState} ${statusClass} ${className}` : `${rootState} ${statusClass}`;

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReducedMotion(media.matches);
    media.addEventListener("change", onChange);
    return () => {
      media.removeEventListener("change", onChange);
    };
  }, []);

  useEffect(() => {
    const onVisibilityChange = () => setDocumentVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return undefined;
    }

    resizeCanvas(canvas);
    paletteRef.current = resolvePalette(canvas);

    const drawStatic = () => {
      for (const string of stringsRef.current) {
        string.amp = 0.03;
      }
      drawScene(canvas, stringsRef.current, [], paletteRef.current, 0, "static");
    };

    const observer = new ResizeObserver(() => {
      resizeCanvas(canvas);
      paletteRef.current = resolvePalette(canvas);
      if (frameRef.current === null) {
        drawStatic();
      }
    });
    observer.observe(canvas);

    const stopAnimation = () => {
      if (frameRef.current !== null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
    };

    // Playback drives the loop: animate only while audio is actually playing
    // and the panel/document are visible with reduced motion off.
    const shouldAnimate = active && playing && documentVisible && !reducedMotion;
    if (canvas.dataset.analyserStatus !== visualizer.status) {
      canvas.dataset.analyserStatus = visualizer.status;
    }
    canvas.dataset.reducedMotion = reducedMotion ? "true" : "false";

    if (!shouldAnimate) {
      stopAnimation();
      drawStatic();
      return () => {
        observer.disconnect();
      };
    }

    const step = (timestamp: number) => {
      frameRef.current = window.requestAnimationFrame(step);
      if (timestamp - lastFrameAtRef.current < FRAME_INTERVAL_MS) {
        return;
      }
      lastFrameAtRef.current = timestamp;
      resizeCanvas(canvas);

      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, canvas.width / ratio);
      const height = Math.max(1, canvas.height / ratio);
      const strings = stringsRef.current;
      const motes = motesRef.current;
      const analyser = playing && visualizer.status === "ready" ? visualizer.analyserRef.current : null;
      const live = Boolean(analyser);
      let energyTotal = 0;

      if (analyser) {
        if (!freqBufferRef.current || freqBufferRef.current.length !== analyser.frequencyBinCount) {
          freqBufferRef.current = new Uint8Array(analyser.frequencyBinCount);
        }
        analyser.getByteFrequencyData(freqBufferRef.current);
      }

      idlePhaseRef.current += 0.012;

      for (let s = 0; s < strings.length; s += 1) {
        const string = strings[s];
        let target: number;
        if (analyser && freqBufferRef.current) {
          const raw = bandLevel(freqBufferRef.current, s);
          target = Math.min(1, Math.pow(raw * 1.5, 1.3));
          // Pluck: sharp attack releases a mote at an antinode.
          if (raw - string.prevBand > 0.16 && motes.length < MAX_MOTES) {
            const antinode = (Math.floor(Math.random() * string.mode) + 0.5) / string.mode;
            motes.push({
              x: antinode * width,
              y: stringBaseY(s, height),
              vx: (Math.random() - 0.5) * 0.5,
              vy: -(0.4 + Math.random() * 0.7),
              life: 1,
              size: 1.4 + Math.random() * 1.4,
            });
          }
          string.prevBand = raw;
        } else {
          // Idle breathing: slow, phase-offset swell.
          target = 0.05 + Math.sin(idlePhaseRef.current + s * 1.1) * 0.03;
          string.prevBand = 0;
        }
        string.amp += (target - string.amp) * 0.3;
        string.phase += string.speed * (0.25 + string.amp * 1.6);
        energyTotal += string.amp;
      }

      for (let i = motes.length - 1; i >= 0; i -= 1) {
        const mote = motes[i];
        mote.x += mote.vx;
        mote.y += mote.vy;
        mote.vy *= 0.985;
        mote.life -= 0.02;
        if (mote.life <= 0 || mote.y < -4) {
          motes.splice(i, 1);
        }
      }

      frameCountRef.current += 1;
      canvas.dataset.waveformFrame = String(frameCountRef.current);
      if (canvas.dataset.analyserStatus !== visualizer.status) {
        canvas.dataset.analyserStatus = visualizer.status;
      }
      drawScene(canvas, strings, motes, paletteRef.current, energyTotal / STRING_COUNT, live ? "live" : "idle");
    };

    frameRef.current = window.requestAnimationFrame(step);
    return () => {
      stopAnimation();
      observer.disconnect();
    };
  }, [active, documentVisible, playing, reducedMotion, visualizer]);

  return (
    <div className={rootClassName} data-playing={playing ? "true" : "false"} data-analyser-status={visualizer.status} aria-hidden="true">
      <canvas
        ref={canvasRef}
        className="now-playing-canvas"
        data-analyser-status={visualizer.status}
        data-reduced-motion={reducedMotion ? "true" : "false"}
        data-rendering="static"
        data-waveform-energy="0"
        data-waveform-frame="0"
      />
    </div>
  );
}

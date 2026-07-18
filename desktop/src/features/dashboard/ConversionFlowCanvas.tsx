import { useEffect, useRef, useState } from "react";

/**
 * Conversion visual: text becoming sound.
 *
 * Letterforms drift down from the top of the card. At a faint membrane
 * line they dissolve, and the energy of each crossing ripples into a
 * waveform ribbon flowing along the bottom — the pipeline itself,
 * drawn: text in, audio out. Idle, a few glyphs hang in slow suspension
 * over a still line.
 */

const FRAME_INTERVAL_MS = 1000 / 30;
const GLYPHS = "ORPHEUSAEIMNTLVY";
const RIBBON_POINTS = 44;
const MAX_GLYPHS_ACTIVE = 16;
const MAX_GLYPHS_IDLE = 3;

interface ConversionFlowCanvasProps {
  active: boolean;
  className?: string;
}

interface GlyphParticle {
  char: string;
  x: number;
  y: number;
  vy: number;
  drift: number;
  size: number;
  born: number;
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

function spawnGlyph(width: number, now: number, idle: boolean): GlyphParticle {
  return {
    char: GLYPHS[Math.floor(Math.random() * GLYPHS.length)],
    x: 6 + Math.random() * Math.max(1, width - 12),
    y: -6 - Math.random() * 14,
    vy: idle ? 0.06 + Math.random() * 0.05 : 0.5 + Math.random() * 0.65,
    drift: (Math.random() - 0.5) * 0.2,
    size: 8 + Math.random() * 5,
    born: now,
  };
}

export function ConversionFlowCanvas({ active, className }: ConversionFlowCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const frameRef = useRef<number | null>(null);
  const lastFrameAtRef = useRef(0);
  const glyphsRef = useRef<GlyphParticle[]>([]);
  const ribbonEnergyRef = useRef<Float32Array>(new Float32Array(RIBBON_POINTS));
  const phaseRef = useRef(0);
  const paletteRef = useRef<Palette>({ accent: "#c9a06c", accentMuted: "#b08d5e", faint: "#5e5a56" });
  const [reducedMotion, setReducedMotion] = useState(() =>
    window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReducedMotion(media.matches);
    media.addEventListener("change", onChange);
    return () => {
      media.removeEventListener("change", onChange);
    };
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return undefined;
    }

    resizeCanvas(canvas);
    paletteRef.current = resolvePalette(canvas);

    const draw = (timestamp: number) => {
      const context = canvas.getContext("2d");
      if (!context) {
        return;
      }
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const width = Math.max(1, canvas.width / ratio);
      const height = Math.max(1, canvas.height / ratio);
      const palette = paletteRef.current;
      const glyphs = glyphsRef.current;
      const energy = ribbonEnergyRef.current;
      const membraneY = height * 0.6;
      const ribbonY = height * 0.82;

      phaseRef.current += active ? 0.09 : 0.02;

      // Spawn.
      const glyphCap = active ? MAX_GLYPHS_ACTIVE : MAX_GLYPHS_IDLE;
      const spawnChance = active ? 0.35 : 0.02;
      if (glyphs.length < glyphCap && Math.random() < spawnChance) {
        glyphs.push(spawnGlyph(width, timestamp, !active));
      }

      // Integrate glyphs; crossings feed the ribbon.
      for (let i = glyphs.length - 1; i >= 0; i -= 1) {
        const glyph = glyphs[i];
        glyph.y += glyph.vy;
        glyph.x += glyph.drift;
        if (glyph.y >= membraneY) {
          const bucket = Math.max(0, Math.min(RIBBON_POINTS - 1, Math.floor((glyph.x / width) * RIBBON_POINTS)));
          energy[bucket] = Math.min(1.6, energy[bucket] + 0.9);
          glyphs.splice(i, 1);
        }
      }

      // Ribbon energy: diffuse to neighbours, then decay.
      const diffused = new Float32Array(RIBBON_POINTS);
      for (let i = 0; i < RIBBON_POINTS; i += 1) {
        const left = energy[Math.max(0, i - 1)];
        const right = energy[Math.min(RIBBON_POINTS - 1, i + 1)];
        diffused[i] = (energy[i] * 0.72 + (left + right) * 0.14) * 0.94;
      }
      ribbonEnergyRef.current = diffused;

      // Paint.
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.textAlign = "center";
      context.textBaseline = "middle";

      // Falling glyphs, warming as they near the membrane.
      for (const glyph of glyphs) {
        const closeness = Math.max(0, Math.min(1, glyph.y / membraneY));
        context.globalAlpha = 0.16 + closeness * 0.6;
        context.fillStyle = closeness > 0.75 ? palette.accent : palette.faint;
        context.font = `${glyph.size}px "JetBrains Mono", ui-monospace, monospace`;
        context.fillText(glyph.char, glyph.x, glyph.y);
      }

      // Membrane.
      const membraneGlow = Math.min(1, diffused.reduce((a, b) => a + b, 0) / 4);
      const gradient = context.createLinearGradient(0, 0, width, 0);
      gradient.addColorStop(0, "transparent");
      gradient.addColorStop(0.5, palette.accentMuted);
      gradient.addColorStop(1, "transparent");
      context.globalAlpha = active ? 0.35 + membraneGlow * 0.4 : 0.18;
      context.strokeStyle = gradient;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(0, membraneY);
      context.lineTo(width, membraneY);
      context.stroke();

      // Waveform ribbon.
      const traceRibbon = (scale: number) => {
        context.beginPath();
        for (let i = 0; i < RIBBON_POINTS; i += 1) {
          const t = i / (RIBBON_POINTS - 1);
          const x = t * width;
          const idleWave = Math.sin(phaseRef.current + t * Math.PI * 3) * (active ? 1.6 : 0.7);
          const y = ribbonY + idleWave - diffused[i] * height * 0.1 * scale * Math.sin(phaseRef.current * 2 + i);
          if (i === 0) {
            context.moveTo(x, y);
          } else {
            context.lineTo(x, y);
          }
        }
      };

      context.lineCap = "round";
      context.lineJoin = "round";
      context.globalAlpha = active ? 0.24 : 0.1;
      context.strokeStyle = palette.accentMuted;
      context.lineWidth = 3.6;
      traceRibbon(0.9);
      context.stroke();

      context.globalAlpha = active ? 0.85 : 0.35;
      context.strokeStyle = active ? palette.accent : palette.faint;
      context.lineWidth = 1.3;
      traceRibbon(1);
      context.stroke();

      context.globalAlpha = 1;
      canvas.dataset.rendering = reducedMotion ? "static" : active ? "active" : "idle";
    };

    const observer = new ResizeObserver(() => {
      resizeCanvas(canvas);
      paletteRef.current = resolvePalette(canvas);
      if (frameRef.current === null) {
        draw(performance.now());
      }
    });
    observer.observe(canvas);

    if (reducedMotion) {
      // One static composition: suspended glyphs over a still line.
      glyphsRef.current = [];
      draw(performance.now());
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
      draw(timestamp);
    };

    frameRef.current = window.requestAnimationFrame(step);
    return () => {
      if (frameRef.current !== null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
      observer.disconnect();
    };
  }, [active, reducedMotion]);

  const rootClassName = className
    ? `conversion-flow${active ? " is-active" : ""} ${className}`
    : `conversion-flow${active ? " is-active" : ""}`;

  return (
    <div className={rootClassName} aria-hidden="true">
      <canvas ref={canvasRef} className="conversion-flow-canvas" data-rendering="idle" />
    </div>
  );
}

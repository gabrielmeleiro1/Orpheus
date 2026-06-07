import { useEffect, useRef, useState } from "react";

import type { AudioAnalyserHandle } from "../player/useAudioAnalyser";

const FRAME_INTERVAL_MS = 1000 / 30;
const POINT_COUNT = 160;

interface AudioWaveformCanvasProps {
  visualizer: AudioAnalyserHandle;
  playing: boolean;
  active: boolean;
  className?: string;
}

interface WaveformColors {
  baseline: string;
  rust: string;
  rustSoft: string;
}

function cssColor(canvas: HTMLCanvasElement, name: string, fallback: string): string {
  return getComputedStyle(canvas).getPropertyValue(name).trim() || fallback;
}

function resolveCanvasColors(canvas: HTMLCanvasElement): WaveformColors {
  return {
    baseline: cssColor(canvas, "--text-soft", "rgba(207, 208, 196, 0.42)"),
    rust: cssColor(canvas, "--accent", "#9b4328"),
    rustSoft: cssColor(canvas, "--accent-soft", "rgba(155, 67, 40, 0.42)"),
  };
}

function calmSample(index: number): number {
  return Math.sin(index * 0.19) * 0.1 + Math.sin(index * 0.047) * 0.06;
}

function ensureSmoothSamples(samplesRef: React.MutableRefObject<Float32Array | null>): Float32Array {
  if (!samplesRef.current || samplesRef.current.length !== POINT_COUNT) {
    samplesRef.current = new Float32Array(POINT_COUNT);
  }
  return samplesRef.current;
}

function resizeCanvas(canvas: HTMLCanvasElement): void {
  const rect = canvas.getBoundingClientRect();
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, Math.round(rect.width * pixelRatio));
  const height = Math.max(1, Math.round(rect.height * pixelRatio));

  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function drawWavePath(
  context: CanvasRenderingContext2D,
  samples: Float32Array,
  width: number,
  height: number,
  amplitude: number,
): void {
  const centerY = height * 0.52;
  context.beginPath();

  for (let index = 0; index < samples.length; index += 1) {
    const x = (index / (samples.length - 1)) * width;
    const y = centerY + samples[index] * amplitude;
    if (index === 0) {
      context.moveTo(x, y);
    } else {
      context.lineTo(x, y);
    }
  }
}

function drawFrame(
  canvas: HTMLCanvasElement,
  samples: Float32Array,
  energy: number,
  rendering: "fallback" | "live" | "paused",
  colors: WaveformColors,
): void {
  const context = canvas.getContext("2d");
  if (!context) {
    return;
  }

  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, canvas.width / pixelRatio);
  const height = Math.max(1, canvas.height / pixelRatio);

  context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  context.clearRect(0, 0, width, height);

  context.globalAlpha = 0.5;
  context.strokeStyle = colors.baseline;
  context.lineWidth = 1;
  context.beginPath();
  context.moveTo(0, height * 0.52);
  context.lineTo(width, height * 0.52);
  context.stroke();

  const amplitude = Math.max(5, Math.min(height * 0.42, height * (0.18 + energy * 0.72)));

  context.lineCap = "round";
  context.lineJoin = "round";

  context.globalAlpha = rendering === "fallback" ? 0.18 : 0.22;
  context.strokeStyle = colors.rustSoft;
  context.lineWidth = 7;
  drawWavePath(context, samples, width, height, amplitude * 0.72);
  context.stroke();

  context.globalAlpha = rendering === "fallback" ? 0.34 : 0.54;
  context.strokeStyle = colors.rustSoft;
  context.lineWidth = 3;
  drawWavePath(context, samples, width, height, amplitude * 0.9);
  context.stroke();

  context.globalAlpha = rendering === "fallback" ? 0.62 : 0.92;
  context.strokeStyle = colors.rust;
  context.lineWidth = 2;
  drawWavePath(context, samples, width, height, amplitude);
  context.stroke();
  context.globalAlpha = 1;

  canvas.dataset.waveformEnergy = energy.toFixed(4);
  canvas.dataset.rendering = rendering;
}

function drawCalmFrame(
  canvas: HTMLCanvasElement,
  samplesRef: React.MutableRefObject<Float32Array | null>,
  rendering: "fallback" | "paused",
  colors: WaveformColors,
): void {
  resizeCanvas(canvas);
  const samples = ensureSmoothSamples(samplesRef);
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] += (calmSample(index) - samples[index]) * 0.2;
  }
  drawFrame(canvas, samples, 0.03, rendering, colors);
}

export function AudioWaveformCanvas({ visualizer, playing, active, className }: AudioWaveformCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animationFrameRef = useRef<number | null>(null);
  const lastFrameAtRef = useRef(0);
  const frameCountRef = useRef(0);
  const sampleBufferRef = useRef<Uint8Array<ArrayBuffer> | null>(null);
  const smoothSamplesRef = useRef<Float32Array | null>(null);
  const colorsRef = useRef<WaveformColors>({
    baseline: "rgba(207, 208, 196, 0.42)",
    rust: "#9b4328",
    rustSoft: "rgba(155, 67, 40, 0.42)",
  });
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
      return;
    }

    resizeCanvas(canvas);
    colorsRef.current = resolveCanvasColors(canvas);
    drawCalmFrame(canvas, smoothSamplesRef, playing && visualizer.status !== "ready" ? "fallback" : "paused", colorsRef.current);

    const observer = new ResizeObserver(() => {
      resizeCanvas(canvas);
      colorsRef.current = resolveCanvasColors(canvas);
      drawCalmFrame(canvas, smoothSamplesRef, playing && visualizer.status !== "ready" ? "fallback" : "paused", colorsRef.current);
    });
    observer.observe(canvas);

    const themeObserver = new MutationObserver(() => {
      colorsRef.current = resolveCanvasColors(canvas);
      drawCalmFrame(canvas, smoothSamplesRef, playing && visualizer.status !== "ready" ? "fallback" : "paused", colorsRef.current);
    });
    themeObserver.observe(document.body, { attributes: true, attributeFilter: ["data-theme"] });

    return () => {
      observer.disconnect();
      themeObserver.disconnect();
    };
  }, [playing, visualizer.status]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    const stopAnimation = (rendering: "fallback" | "paused") => {
      if (animationFrameRef.current !== null) {
        window.cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }
      drawCalmFrame(canvas, smoothSamplesRef, rendering, colorsRef.current);
    };

    const shouldDrawLive =
      active && playing && !reducedMotion && documentVisible && visualizer.status === "ready";

    canvas.dataset.analyserStatus = visualizer.status;
    canvas.dataset.reducedMotion = reducedMotion ? "true" : "false";
    canvas.dataset.waveformFrame = String(frameCountRef.current);

    if (!shouldDrawLive) {
      stopAnimation(playing && visualizer.status !== "ready" ? "fallback" : "paused");
      return undefined;
    }

    const drawLive = (timestamp: number) => {
      if (timestamp - lastFrameAtRef.current < FRAME_INTERVAL_MS) {
        animationFrameRef.current = window.requestAnimationFrame(drawLive);
        return;
      }

      const analyser = visualizer.analyserRef.current;
      if (!analyser) {
        stopAnimation("fallback");
        return;
      }

      resizeCanvas(canvas);
      if (!sampleBufferRef.current || sampleBufferRef.current.length !== analyser.fftSize) {
        sampleBufferRef.current = new Uint8Array(analyser.fftSize);
      }

      analyser.getByteTimeDomainData(sampleBufferRef.current);
      const samples = ensureSmoothSamples(smoothSamplesRef);
      let energyTotal = 0;

      for (let index = 0; index < samples.length; index += 1) {
        const sourceIndex = Math.floor((index / (samples.length - 1)) * (sampleBufferRef.current.length - 1));
        const centered = (sampleBufferRef.current[sourceIndex] - 128) / 128;
        samples[index] += (centered - samples[index]) * 0.28;
        energyTotal += Math.abs(samples[index]);
      }

      const energy = energyTotal / samples.length;
      frameCountRef.current += 1;
      lastFrameAtRef.current = timestamp;
      canvas.dataset.waveformFrame = String(frameCountRef.current);
      canvas.dataset.analyserStatus = visualizer.status;
      canvas.dataset.reducedMotion = "false";
      drawFrame(canvas, samples, energy, "live", colorsRef.current);
      animationFrameRef.current = window.requestAnimationFrame(drawLive);
    };

    animationFrameRef.current = window.requestAnimationFrame(drawLive);
    return () => {
      if (animationFrameRef.current !== null) {
        window.cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }
    };
  }, [active, documentVisible, playing, reducedMotion, visualizer]);

  return (
    <div className={rootClassName} data-playing={playing ? "true" : "false"} data-analyser-status={visualizer.status} aria-hidden="true">
      <canvas
        ref={canvasRef}
        className="audio-waveform-canvas"
        data-analyser-status={visualizer.status}
        data-reduced-motion={reducedMotion ? "true" : "false"}
        data-rendering="paused"
        data-waveform-energy="0"
        data-waveform-frame="0"
      />
    </div>
  );
}

import { useEffect, useMemo, useRef, useState, type MutableRefObject, type RefObject } from "react";

export type AudioAnalyserStatus = "idle" | "ready" | "blocked" | "unsupported";

export interface AudioAnalyserHandle {
  analyserRef: MutableRefObject<AnalyserNode | null>;
  status: AudioAnalyserStatus;
}

interface AudioAnalyserBundle {
  context: AudioContext;
  source: MediaElementAudioSourceNode;
  analyser: AnalyserNode;
  closeTimer: number | null;
}

interface WebKitAudioWindow extends Window {
  webkitAudioContext?: typeof AudioContext;
}

const audioAnalyserBundles = new WeakMap<HTMLMediaElement, AudioAnalyserBundle>();

function getAudioContextConstructor(): typeof AudioContext | null {
  return window.AudioContext ?? (window as WebKitAudioWindow).webkitAudioContext ?? null;
}

function isClosed(context: AudioContext): boolean {
  return context.state === "closed";
}

function disconnectBundle(bundle: AudioAnalyserBundle): void {
  try {
    bundle.source.disconnect();
  } catch {
    // Source may already be disconnected during media teardown.
  }

  try {
    bundle.analyser.disconnect();
  } catch {
    // Analyser may already be disconnected during media teardown.
  }
}

function cancelScheduledClose(bundle: AudioAnalyserBundle): void {
  if (bundle.closeTimer === null) {
    return;
  }

  window.clearTimeout(bundle.closeTimer);
  bundle.closeTimer = null;
}

function scheduleBundleClose(audio: HTMLMediaElement, bundle: AudioAnalyserBundle): void {
  cancelScheduledClose(bundle);

  bundle.closeTimer = window.setTimeout(() => {
    bundle.closeTimer = null;
    if (!audio.isConnected) {
      audioAnalyserBundles.delete(audio);
      disconnectBundle(bundle);

      if (!isClosed(bundle.context)) {
        void bundle.context.close().catch(() => undefined);
      }
      return;
    }

    if (bundle.context.state === "running") {
      void bundle.context.suspend().catch(() => undefined);
    }
  }, 250);
}

export function useAudioAnalyser(
  audioRef: RefObject<HTMLAudioElement | null>,
  playing: boolean,
): AudioAnalyserHandle {
  const analyserRef = useRef<AnalyserNode | null>(null);
  const ownedAudioRef = useRef<HTMLAudioElement | null>(null);
  const [status, setStatus] = useState<AudioAnalyserStatus>("idle");

  useEffect(() => {
    const audio = audioRef.current;

    if (!audio) {
      analyserRef.current = null;
      setStatus("idle");
      return;
    }

    const AudioContextConstructor = getAudioContextConstructor();
    if (!AudioContextConstructor) {
      analyserRef.current = null;
      setStatus("unsupported");
      return;
    }

    const ensureBundle = (): AudioAnalyserBundle | null => {
      const cached = audioAnalyserBundles.get(audio);
      if (cached && !isClosed(cached.context)) {
        cancelScheduledClose(cached);
        analyserRef.current = cached.analyser;
        ownedAudioRef.current = audio;
        setStatus("ready");
        return cached;
      }

      try {
        const context = new AudioContextConstructor();
        const source = context.createMediaElementSource(audio);
        const analyser = context.createAnalyser();
        analyser.fftSize = 1024;
        analyser.smoothingTimeConstant = 0.82;
        source.connect(analyser);
        analyser.connect(context.destination);

        const bundle = { context, source, analyser, closeTimer: null };
        audioAnalyserBundles.set(audio, bundle);
        analyserRef.current = analyser;
        ownedAudioRef.current = audio;
        setStatus("ready");
        return bundle;
      } catch {
        analyserRef.current = null;
        setStatus("blocked");
        return null;
      }
    };

    const bundle = ensureBundle();
    if (!bundle) {
      return;
    }

    if (!playing) {
      if (bundle.context.state === "running") {
        void bundle.context.suspend().catch(() => undefined);
      }
      return;
    }

    if (bundle.context.state === "suspended") {
      void bundle.context.resume().catch(() => {
        analyserRef.current = null;
        setStatus("blocked");
      });
    }
  }, [audioRef, playing]);

  useEffect(() => {
    return () => {
      const audio = ownedAudioRef.current;
      if (!audio) {
        return;
      }

      const bundle = audioAnalyserBundles.get(audio);
      if (!bundle) {
        return;
      }

      analyserRef.current = null;
      scheduleBundleClose(audio, bundle);
    };
  }, []);

  return useMemo(() => ({ analyserRef, status }), [status]);
}

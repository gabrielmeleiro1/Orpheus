// @vitest-environment happy-dom

import { act, StrictMode, useRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useAudioAnalyser } from "./useAudioAnalyser";

interface MockAudioContext {
  state: AudioContextState;
  destination: AudioDestinationNode;
  createMediaElementSource: ReturnType<typeof vi.fn>;
  createAnalyser: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  suspend: ReturnType<typeof vi.fn>;
  resume: ReturnType<typeof vi.fn>;
}

interface MountedProbeProps {
  playing: boolean;
}

function MountedProbe({ playing }: MountedProbeProps): JSX.Element {
  const audioRef = useRef<HTMLAudioElement>(null);
  const { status } = useAudioAnalyser(audioRef, playing);
  return <audio ref={audioRef} data-analyser-status={status} />;
}

function setUserAgent(userAgent: string): void {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
}

function installAudioContextMocks(): {
  AudioContextConstructor: ReturnType<typeof vi.fn>;
  context: MockAudioContext;
  source: { connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> };
  analyser: {
    fftSize: number;
    smoothingTimeConstant: number;
    connect: ReturnType<typeof vi.fn>;
    disconnect: ReturnType<typeof vi.fn>;
  };
} {
  const source = {
    connect: vi.fn(),
    disconnect: vi.fn(),
  };
  const analyser = {
    fftSize: 0,
    smoothingTimeConstant: 0,
    connect: vi.fn(),
    disconnect: vi.fn(),
  };
  const context: MockAudioContext = {
    state: "suspended",
    destination: {} as AudioDestinationNode,
    createMediaElementSource: vi.fn(() => source),
    createAnalyser: vi.fn(() => analyser),
    close: vi.fn(async () => undefined),
    suspend: vi.fn(async () => undefined),
    resume: vi.fn(async () => undefined),
  };
  const AudioContextConstructor = vi.fn(function MockAudioContextConstructor() {
    return context;
  });
  Object.defineProperty(window, "AudioContext", {
    configurable: true,
    value: AudioContextConstructor,
  });
  Object.defineProperty(window, "webkitAudioContext", {
    configurable: true,
    value: AudioContextConstructor,
  });
  return { AudioContextConstructor, context, source, analyser };
}

describe("mounted useAudioAnalyser", () => {
  let container: HTMLDivElement;
  let root: Root;
  let originalAudioContext: PropertyDescriptor | undefined;
  let originalWebkitAudioContext: PropertyDescriptor | undefined;

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    originalAudioContext = Object.getOwnPropertyDescriptor(window, "AudioContext");
    originalWebkitAudioContext = Object.getOwnPropertyDescriptor(window, "webkitAudioContext");
    vi.useFakeTimers();
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    if (originalAudioContext) {
      Object.defineProperty(window, "AudioContext", originalAudioContext);
    } else {
      delete (window as Window & { AudioContext?: typeof AudioContext }).AudioContext;
    }
    if (originalWebkitAudioContext) {
      Object.defineProperty(window, "webkitAudioContext", originalWebkitAudioContext);
    } else {
      delete (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    }
    vi.restoreAllMocks();
  });

  it.each([
    [
      "macOS WKWebView",
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 " +
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    ],
    [
      "Linux WebKitGTK",
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) " +
        "Version/17.0 Safari/605.1.15",
    ],
  ])("does not construct or route an analyser in %s", async (_engine, userAgent) => {
    setUserAgent(userAgent);
    const { AudioContextConstructor, context } = installAudioContextMocks();

    await act(async () => {
      root.render(<MountedProbe playing />);
    });

    expect(container.querySelector("audio")?.dataset.analyserStatus).toBe("unsupported");
    expect(AudioContextConstructor).not.toHaveBeenCalled();
    expect(context.createMediaElementSource).not.toHaveBeenCalled();
  });

  it("constructs one Blink analyser graph and fully cleans it up after StrictMode replay", async () => {
    setUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    );
    const { AudioContextConstructor, context, source, analyser } = installAudioContextMocks();

    await act(async () => {
      root.render(
        <StrictMode>
          <MountedProbe playing />
        </StrictMode>,
      );
    });

    expect(container.querySelector("audio")?.dataset.analyserStatus).toBe("ready");
    expect(AudioContextConstructor).toHaveBeenCalledOnce();
    expect(context.createMediaElementSource).toHaveBeenCalledOnce();
    expect(source.connect).toHaveBeenCalledWith(analyser);
    expect(analyser.connect).toHaveBeenCalledWith(context.destination);

    await act(async () => root.unmount());
    container.remove();
    await act(async () => {
      vi.advanceTimersByTime(250);
    });

    expect(source.disconnect).toHaveBeenCalledOnce();
    expect(analyser.disconnect).toHaveBeenCalledOnce();
    expect(context.close).toHaveBeenCalledOnce();
  });
});

// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import { useGlobalShortcuts } from "./useGlobalShortcuts";

it("ignores IME composition and repeated actions while keeping held-arrow seeking", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const start = vi.fn();
  const togglePlay = vi.fn(async () => {});
  const playRelative = vi.fn(async () => {});
  const refreshLibrary = vi.fn(async () => {});
  const seekBy = vi.fn();
  function Probe({ startEnabled = true }: { startEnabled?: boolean }) {
    useGlobalShortcuts({ activeTab: "convert", startEnabled, startCommandRef: { current: start },
      togglePlay, playRelative, refreshLibrary, seekBy });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe />));
    for (const key of ["s", " ", "r", "n", "p", "ArrowLeft"]) {
      document.dispatchEvent(new KeyboardEvent("keydown", { key, isComposing: true, bubbles: true }));
    }
    for (const key of ["s", " ", "r", "n", "p"]) {
      document.dispatchEvent(new KeyboardEvent("keydown", { key, repeat: true, bubbles: true }));
    }
    for (const command of [start, togglePlay, playRelative, refreshLibrary, seekBy]) expect(command).not.toHaveBeenCalled();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", repeat: true, bubbles: true }));
    expect(seekBy).toHaveBeenCalledWith(10);
    await act(async () => root.render(<Probe startEnabled={false} />));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "s", bubbles: true }));
    expect(start).not.toHaveBeenCalled();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true }));
    expect(togglePlay).toHaveBeenCalledOnce();
  } finally {
    await act(async () => root.unmount());
    vi.unstubAllGlobals();
  }
});

it("suspends workspace shortcuts while model management is visible", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const refreshLibrary = vi.fn(async () => {});
  const togglePlay = vi.fn(async () => {});
  const seekBy = vi.fn();
  const playRelative = vi.fn(async () => {});
  const start = vi.fn();
  const startCommandRef = { current: start };
  function Probe({ enabled }: { enabled: boolean }) {
    useGlobalShortcuts({ enabled, activeTab: "convert", refreshLibrary, togglePlay, seekBy, playRelative, startCommandRef });
    return null;
  }
  const root = createRoot(document.createElement("div"));
  try {
    await act(async () => root.render(<Probe enabled={false} />));
    for (const key of ["s", "r", " ", "ArrowLeft", "ArrowRight", "n", "p"]) {
      const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
      document.dispatchEvent(event);
      expect(event.defaultPrevented).toBe(false);
    }
    for (const command of [start, refreshLibrary, togglePlay, seekBy, playRelative]) expect(command).not.toHaveBeenCalled();
    await act(async () => root.render(<Probe enabled={true} />));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "s", bubbles: true }));
    expect(start).toHaveBeenCalledOnce();
  } finally {
    await act(async () => root.unmount());
    vi.unstubAllGlobals();
  }
});

it("leaves source controls and already handled keys to their local actions", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const togglePlay = vi.fn(async () => {});
  const browse = vi.fn();
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  function Probe() {
    useGlobalShortcuts({ activeTab: "convert", refreshLibrary: vi.fn(async () => {}), togglePlay,
      seekBy: vi.fn(), playRelative: vi.fn(async () => {}), startCommandRef: { current: vi.fn() } });
    return <>
      <div role="button" tabIndex={0} onKeyDown={(event) => { event.preventDefault(); browse(); }}>
        <span data-target="drop-zone">Browse</span>
      </div>
      <details><summary><span data-target="disclosure">Enter path manually</span></summary></details>
      <select data-target="select"><option>Voice</option></select>
      <div data-target="handled" tabIndex={0} onKeyDown={(event) => event.preventDefault()}>Custom control</div>
    </>;
  }
  try {
    await act(async () => root.render(<Probe />));
    for (const name of ["drop-zone", "disclosure", "select", "handled"]) {
      const event = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
      container.querySelector(`[data-target="${name}"]`)!.dispatchEvent(event);
      if (name === "disclosure" || name === "select") expect(event.defaultPrevented).toBe(false);
    }
    expect(browse).toHaveBeenCalledOnce();
    expect(togglePlay).not.toHaveBeenCalled();

    document.dispatchEvent(new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true }));
    expect(togglePlay).toHaveBeenCalledOnce();
  } finally {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  }
});

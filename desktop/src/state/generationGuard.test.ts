// @vitest-environment happy-dom

import { act, createElement, StrictMode, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useLatestRequest } from "../features/conversion/useLatestRequest";
import { createGenerationGuard, useGenerationGuard, useMountedRef } from "./generationGuard";

describe("generation guard", () => {
  it("keeps captures valid until invalidation and never revives old captures", () => {
    const guard = createGenerationGuard();
    const first = guard.capture();
    const alsoFirst = guard.capture();
    expect(guard.current).toBe(0);
    expect(first()).toBe(true);
    expect(alsoFirst()).toBe(true);
    guard.invalidate();
    const second = guard.capture();
    expect(guard.current).toBe(1);
    expect(first()).toBe(false);
    expect(alsoFirst()).toBe(false);
    expect(second()).toBe(true);
    guard.invalidate();
    expect(first()).toBe(false);
    expect(second()).toBe(false);
    expect(guard.capture()()).toBe(true);
  });

  it("isolates independent guards and retains mutable ref compatibility", () => {
    const first = createGenerationGuard();
    const second = createGenerationGuard();
    const ownsFirst = first.capture();
    const ownsSecond = second.capture();
    first.current += 1;
    expect(ownsFirst()).toBe(false);
    expect(ownsSecond()).toBe(true);
  });
});

describe("ownership hook lifetimes", () => {
  let root: Root;
  let container: HTMLDivElement;
  let guard: ReturnType<typeof useGenerationGuard>;
  let mounted: ReturnType<typeof useMountedRef>;
  let beginRequest: ReturnType<typeof useLatestRequest>;
  let mountedDuringRender: boolean;

  function Probe({ initialMounted = false }: { initialMounted?: boolean }) {
    guard = useGenerationGuard();
    mounted = useMountedRef(initialMounted);
    mountedDuringRender = mounted.current;
    beginRequest = useLatestRequest();
    return null;
  }

  beforeEach(() => {
    (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
  });

  it("preserves guard and callback identity on rerender and leaves unmount invalidation explicit", async () => {
    await act(async () => root.render(createElement(Probe)));
    const originalGuard = guard;
    const originalBegin = beginRequest;
    const originalMounted = mounted;
    const isCurrent = guard.capture();
    const isLatest = beginRequest();
    await act(async () => root.render(createElement(Probe)));
    expect(guard).toBe(originalGuard);
    expect(beginRequest).toBe(originalBegin);
    expect(mounted).toBe(originalMounted);
    expect(isCurrent()).toBe(true);
    expect(isLatest()).toBe(true);
    await act(async () => root.unmount());
    expect(isCurrent()).toBe(true);
    expect(isLatest()).toBe(false);
    expect(mounted.current).toBe(false);
  });

  it("supersedes only earlier requests", async () => {
    await act(async () => root.render(createElement(Probe)));
    const first = beginRequest();
    const second = beginRequest();
    expect(first()).toBe(false);
    expect(second()).toBe(true);
    expect(guard.current).toBe(0);
  });

  it.each([false, true])("preserves the initial mounted value %s before effects", async (initialMounted) => {
    await act(async () => root.render(createElement(Probe, { initialMounted })));
    expect(mountedDuringRender).toBe(initialMounted);
    expect(mounted.current).toBe(true);
  });

  it("restores the mounted flag after StrictMode effect replay", async () => {
    await act(async () => root.render(createElement(StrictMode, null, createElement(Probe))));
    expect(mounted.current).toBe(true);
    const isLatest = beginRequest();
    await act(async () => root.unmount());
    expect(mounted.current).toBe(false);
    expect(isLatest()).toBe(false);
  });

  it("clears the mounted flag before adjacent teardown and fences requests across effect replay", async () => {
    const mountedAtCleanup: boolean[] = [];
    const requests: Array<() => boolean> = [];
    function EffectProbe() {
      const mountedRef = useMountedRef(true);
      const begin = useLatestRequest();
      useEffect(() => {
        requests.push(begin());
        return () => { mountedAtCleanup.push(mountedRef.current); };
      }, []);
      return null;
    }

    await act(async () => root.render(createElement(StrictMode, null, createElement(EffectProbe))));
    expect(mountedAtCleanup).toEqual([false]);
    expect(requests).toHaveLength(2);
    expect(requests[0]()).toBe(false);
    expect(requests[1]()).toBe(true);
    await act(async () => root.unmount());
    expect(mountedAtCleanup).toEqual([false, false]);
    expect(requests[1]()).toBe(false);
  });
});

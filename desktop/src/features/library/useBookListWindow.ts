import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { computeVirtualWindow, scrollOffsetForIndex, type VirtualWindow } from "./virtualWindow";

// Matches `.book-row` min-height (56px) with no list gap. Only used until the
// real stride is measured from the DOM, and as the estimate whenever the list
// is too short to measure a row-to-row delta.
const ESTIMATED_ROW_STRIDE = 56;
const LIBRARY_GRID_GAP = 0;
const DEFAULT_OVERSCAN = 6;

interface UseBookListWindowArgs {
  itemCount: number;
  /** Index that must stay visible (selected book); pass -1 to disable. */
  selectedIndex: number;
  overscan?: number;
}

interface UseBookListWindowResult {
  containerRef: React.RefObject<HTMLDivElement>;
  window: VirtualWindow;
}

/**
 * Bounded, dependency-free virtualisation for the library list. The scroll
 * viewport is measured live (scroll + resize); the row stride is measured from
 * two rendered rows and falls back to a CSS-derived estimate. Rows outside the
 * window are replaced by top/bottom spacers so the scrollbar and total height
 * stay faithful while DOM node count stays bounded.
 */
export function useBookListWindow({
  itemCount,
  selectedIndex,
  overscan = DEFAULT_OVERSCAN,
}: UseBookListWindowArgs): UseBookListWindowResult {
  const containerRef = useRef<HTMLDivElement>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);
  const [rowStride, setRowStride] = useState(ESTIMATED_ROW_STRIDE);

  const readViewport = useCallback(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    setScrollTop(container.scrollTop);
    setViewportHeight(container.clientHeight);
  }, []);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }

    readViewport();
    const onScroll = () => setScrollTop(container.scrollTop);
    container.addEventListener("scroll", onScroll, { passive: true });

    let observer: ResizeObserver | undefined;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => setViewportHeight(container.clientHeight));
      observer.observe(container);
    }

    return () => {
      container.removeEventListener("scroll", onScroll);
      observer?.disconnect();
    };
  }, [readViewport]);

  const window = computeVirtualWindow(
    itemCount,
    rowStride,
    scrollTop,
    viewportHeight,
    overscan,
    LIBRARY_GRID_GAP,
  );

  // Measure the real row stride from two consecutive rendered rows. Using the
  // offset delta captures the row height and the inter-row gap in one number.
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (!container) {
      return;
    }
    const rows = container.querySelectorAll<HTMLElement>(".book-row");
    if (rows.length < 2) {
      return;
    }
    const measured = rows[1].offsetTop - rows[0].offsetTop;
    if (measured > 0 && Math.abs(measured - rowStride) > 0.5) {
      setRowStride(measured);
    }
  }, [itemCount, window.startIndex, window.endIndex, rowStride]);

  // Keep the selected row in view as the selection moves (keyboard, pager).
  useEffect(() => {
    const container = containerRef.current;
    if (!container || selectedIndex < 0) {
      return;
    }
    const target = scrollOffsetForIndex(selectedIndex, rowStride, container.scrollTop, container.clientHeight);
    if (target !== null) {
      container.scrollTop = target;
      setScrollTop(target);
    }
  }, [selectedIndex, rowStride]);

  return { containerRef, window };
}

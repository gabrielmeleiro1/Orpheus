/**
 * Fixed-stride list virtualisation math, kept as a pure function so the
 * windowing logic can be unit-tested without a layout engine.
 *
 * The library rows are single-line and uniform height (see `.book-row` in
 * library.css), so a constant row stride plus overscan is a robust, dependency
 * free windowing strategy. When the viewport cannot be measured (server render,
 * happy-dom tests, a collapsed panel), an estimated bounded window avoids a
 * full-list DOM spike.
 */

export interface VirtualWindow {
  /** First item index to render (inclusive). */
  startIndex: number;
  /** Last item index to render (exclusive). */
  endIndex: number;
  /** Spacer height above the rendered slice, in pixels. */
  paddingTop: number;
  /** Spacer height below the rendered slice, in pixels. */
  paddingBottom: number;
}

export function computeVirtualWindow(
  itemCount: number,
  rowStride: number,
  scrollTop: number,
  viewportHeight: number,
  overscan: number,
  gridGap = 0,
): VirtualWindow {
  if (itemCount <= 0) {
    return { startIndex: 0, endIndex: 0, paddingTop: 0, paddingBottom: 0 };
  }

  // A hidden panel reports a zero viewport and test/first-paint environments
  // may not expose row measurements yet. Keep an estimated window in those
  // cases; rendering the whole collection would recreate the very DOM spike
  // this helper exists to prevent.
  const effectiveRowStride = rowStride > 0 ? rowStride : 50;
  const effectiveViewportHeight = viewportHeight > 0
    ? viewportHeight
    : effectiveRowStride * 10;

  const safeScrollTop = Math.max(0, scrollTop);
  const safeOverscan = Math.max(0, Math.floor(overscan));

  const firstVisible = Math.floor(safeScrollTop / effectiveRowStride);
  const startIndex = Math.max(0, firstVisible - safeOverscan);

  const visibleCount = Math.ceil(effectiveViewportHeight / effectiveRowStride) + 1;
  const endIndex = Math.min(itemCount, firstVisible + visibleCount + safeOverscan);

  // Each spacer is itself a grid child, so CSS contributes a gap beside it.
  // Remove that one gap from each non-empty spacer to preserve exact height.
  const safeGridGap = Math.max(0, gridGap);
  const paddingTop = Math.max(0, startIndex * effectiveRowStride - (startIndex > 0 ? safeGridGap : 0));
  const rowsBelow = itemCount - endIndex;
  const paddingBottom = Math.max(
    0,
    rowsBelow * effectiveRowStride - (rowsBelow > 0 ? safeGridGap : 0),
  );

  return { startIndex, endIndex, paddingTop, paddingBottom };
}

/**
 * Given a target row index, return the scrollTop that brings it fully into the
 * viewport, or null when it already is. Used to keep the selected/current book
 * visible as the selection moves via keyboard or the pager.
 */
export function scrollOffsetForIndex(
  index: number,
  rowStride: number,
  scrollTop: number,
  viewportHeight: number,
): number | null {
  if (index < 0 || !(rowStride > 0) || !(viewportHeight > 0)) {
    return null;
  }

  const rowTop = index * rowStride;
  const rowBottom = rowTop + rowStride;
  if (rowTop < scrollTop) {
    return rowTop;
  }
  if (rowBottom > scrollTop + viewportHeight) {
    return rowBottom - viewportHeight;
  }
  return null;
}

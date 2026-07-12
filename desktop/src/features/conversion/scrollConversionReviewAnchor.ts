/** Scroll a conversion review anchor into view after a source is prepared. */
export function scrollConversionReviewAnchor(
  element: HTMLElement | null,
  options?: { reducedMotion?: boolean },
): boolean {
  if (!element || typeof element.scrollIntoView !== "function") {
    return false;
  }
  const reducedMotion =
    options?.reducedMotion ??
    (typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  element.scrollIntoView({
    block: "start",
    behavior: reducedMotion ? "auto" : "smooth",
  });
  return true;
}

/**
 * Decide whether to scroll for a newly prepared source.
 * While the panel is hidden, do not scroll and do not consume the ready key so
 * the deliberate review scroll still happens once when the panel becomes visible.
 */
export function nextConversionReviewScrollState(input: {
  readyKey: string;
  acknowledgedReadyKey: string;
  hidden: boolean;
}): { shouldScroll: boolean; acknowledgedReadyKey: string } {
  if (!input.readyKey || input.readyKey === input.acknowledgedReadyKey) {
    return {
      shouldScroll: false,
      acknowledgedReadyKey: input.acknowledgedReadyKey,
    };
  }
  if (input.hidden) {
    return {
      shouldScroll: false,
      acknowledgedReadyKey: input.acknowledgedReadyKey,
    };
  }
  return {
    shouldScroll: true,
    acknowledgedReadyKey: input.readyKey,
  };
}

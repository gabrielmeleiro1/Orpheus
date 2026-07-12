import { describe, expect, it, vi } from "vitest";

import {
  nextConversionReviewScrollState,
  scrollConversionReviewAnchor,
} from "./scrollConversionReviewAnchor";

describe("scrollConversionReviewAnchor", () => {
  it("scrolls the review anchor with reduced-motion-safe behavior", () => {
    const scrollIntoView = vi.fn();
    const element = { scrollIntoView } as unknown as HTMLElement;
    expect(scrollConversionReviewAnchor(element, { reducedMotion: true })).toBe(true);
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "start", behavior: "auto" });
  });

  it("returns false when the anchor is missing", () => {
    expect(scrollConversionReviewAnchor(null)).toBe(false);
  });
});

describe("nextConversionReviewScrollState", () => {
  it("defers scroll while hidden, then scrolls once when shown with the same ready key", () => {
    const readyKey = "/tmp/book.pdf|Prepared Title|1";

    const whileHidden = nextConversionReviewScrollState({
      readyKey,
      acknowledgedReadyKey: "",
      hidden: true,
    });
    expect(whileHidden).toEqual({
      shouldScroll: false,
      acknowledgedReadyKey: "",
    });

    const whenShown = nextConversionReviewScrollState({
      readyKey,
      acknowledgedReadyKey: whileHidden.acknowledgedReadyKey,
      hidden: false,
    });
    expect(whenShown).toEqual({
      shouldScroll: true,
      acknowledgedReadyKey: readyKey,
    });

    const alreadyAcknowledged = nextConversionReviewScrollState({
      readyKey,
      acknowledgedReadyKey: whenShown.acknowledgedReadyKey,
      hidden: false,
    });
    expect(alreadyAcknowledged).toEqual({
      shouldScroll: false,
      acknowledgedReadyKey: readyKey,
    });
  });
});

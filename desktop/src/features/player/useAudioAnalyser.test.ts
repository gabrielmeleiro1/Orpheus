import { afterEach, describe, expect, it, vi } from "vitest";

import { mediaElementRoutingIsReliable } from "./useAudioAnalyser";

function setUserAgent(ua: string): void {
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(ua);
}

describe("mediaElementRoutingIsReliable", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("enables routing on Blink engines (Chrome / Edge / WebView2 / dev preview)", () => {
    setUserAgent(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    );
    expect(mediaElementRoutingIsReliable()).toBe(true);
  });

  it("enables routing on Windows WebView2 (Edg/ token still carries Chrome/)", () => {
    setUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    );
    expect(mediaElementRoutingIsReliable()).toBe(true);
  });

  it("disables routing on macOS WKWebView (Safari-like UA, no Chrome/ token)", () => {
    setUserAgent(
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 " +
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    );
    expect(mediaElementRoutingIsReliable()).toBe(false);
  });

  it("disables routing on Linux WebKitGTK (no Chrome/ token)", () => {
    setUserAgent(
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/605.1.15 (KHTML, like Gecko) " +
        "Version/17.0 Safari/605.1.15",
    );
    expect(mediaElementRoutingIsReliable()).toBe(false);
  });
});

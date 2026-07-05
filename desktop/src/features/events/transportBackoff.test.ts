import { describe, expect, it } from "vitest";

import {
  createTransportBackoff,
  shouldRefreshQueryToken,
} from "./transportBackoff";

describe("event stream transport backoff", () => {
  it("uses exponential full-jitter delays and resets attempts", () => {
    const backoff = createTransportBackoff({ random: () => 0.5 });

    expect(backoff.nextDelayMs()).toBe(250);
    expect(backoff.nextDelayMs()).toBe(500);
    expect(backoff.nextDelayMs()).toBe(1000);

    backoff.reset();

    expect(backoff.nextDelayMs()).toBe(250);
  });

  it("caps exponential delay before applying jitter", () => {
    const backoff = createTransportBackoff({ random: () => 1 });

    const delays = Array.from({ length: 8 }, () => backoff.nextDelayMs());

    expect(delays[delays.length - 1]).toBe(15000);
  });
});

describe("event stream query token refresh decisions", () => {
  it("refreshes missing, expired, and near-expiry desktop query tokens", () => {
    expect(shouldRefreshQueryToken(null, 1_000)).toBe(true);
    expect(shouldRefreshQueryToken({ token: "events", expiresAt: 1_000 }, 1_000)).toBe(true);
    expect(shouldRefreshQueryToken({ token: "events", expiresAt: 31_000 }, 1_000)).toBe(true);
  });

  it("keeps valid query tokens that are not near expiry", () => {
    expect(shouldRefreshQueryToken({ token: "events", expiresAt: 32_000 }, 1_000)).toBe(false);
  });
});

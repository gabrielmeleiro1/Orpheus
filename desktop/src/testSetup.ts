import { beforeEach } from "vitest";

// Node 24 exposes native Web Storage globals. Vitest preserves those globals
// when installing Happy DOM, leaving localStorage undefined without a Node
// backing file. DOM tests need isolated browser storage for both storage areas.
if (typeof window !== "undefined") {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: new Storage(),
    });
  }
}

// Saved state (the Create queue, playback positions) must not carry from one
// test into the next within a file.
if (typeof window !== "undefined") {
  beforeEach(() => {
    globalThis.localStorage?.clear();
    globalThis.sessionStorage?.clear();
  });
}

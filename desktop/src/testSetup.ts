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

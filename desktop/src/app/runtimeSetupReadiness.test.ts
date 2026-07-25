import { describe, expect, it } from "vitest";

import {
  readRuntimeSetupReadyHint,
  writeRuntimeSetupReadyHint,
} from "./runtimeSetupReadiness";

function memoryStorage(initial?: string): Pick<Storage, "getItem" | "setItem" | "removeItem"> {
  let value = initial ?? null;
  return {
    getItem: () => value,
    setItem: (_key, next) => { value = next; },
    removeItem: () => { value = null; },
  };
}

describe("runtime setup readiness hint", () => {
  it("round-trips only the current ready schema", () => {
    const storage = memoryStorage();
    expect(readRuntimeSetupReadyHint(storage)).toBe(false);

    writeRuntimeSetupReadyHint(true, storage);
    expect(readRuntimeSetupReadyHint(storage)).toBe(true);

    writeRuntimeSetupReadyHint(false, storage);
    expect(readRuntimeSetupReadyHint(storage)).toBe(false);
  });

  it("ignores malformed, stale, and inaccessible storage", () => {
    expect(readRuntimeSetupReadyHint(memoryStorage("not-json"))).toBe(false);
    expect(readRuntimeSetupReadyHint(memoryStorage('{"version":0,"ready":true}'))).toBe(false);
    expect(readRuntimeSetupReadyHint({
      getItem: () => { throw new Error("blocked"); },
      setItem: () => undefined,
      removeItem: () => undefined,
    })).toBe(false);
  });
});

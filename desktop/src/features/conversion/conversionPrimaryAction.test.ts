import { describe, expect, it } from "vitest";

import { resolveConversionPrimaryAction } from "./conversionPrimaryAction";

describe("resolveConversionPrimaryAction", () => {
  it("gives single-source mode ownership of start conversion", () => {
    expect(
      resolveConversionPrimaryAction({
        queueItemCount: 0,
        canStartConversion: true,
        canRunQueue: false,
      }),
    ).toEqual({
      primary: "start_conversion",
      showRunQueue: false,
      showStartConversion: true,
      showPreflightConfirmButton: false,
    });

    expect(
      resolveConversionPrimaryAction({
        queueItemCount: 1,
        canStartConversion: true,
        canRunQueue: true,
      }),
    ).toEqual({
      primary: "start_conversion",
      showRunQueue: false,
      showStartConversion: true,
      showPreflightConfirmButton: false,
    });
  });

  it("gives queue mode ownership of run queue", () => {
    expect(
      resolveConversionPrimaryAction({
        queueItemCount: 2,
        canStartConversion: true,
        canRunQueue: true,
      }),
    ).toEqual({
      primary: "run_queue",
      showRunQueue: true,
      showStartConversion: false,
      showPreflightConfirmButton: false,
    });
  });

  it("never surfaces the redundant preflight confirm CTA", () => {
    const result = resolveConversionPrimaryAction({
      queueItemCount: 1,
      canStartConversion: false,
      canRunQueue: false,
    });
    expect(result.showPreflightConfirmButton).toBe(false);
  });
});

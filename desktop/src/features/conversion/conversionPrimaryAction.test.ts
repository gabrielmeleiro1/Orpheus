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
    ).toMatchObject({
      primary: "start_conversion",
      showRunQueue: false,
      showStartConversion: true,
    });

    expect(
      resolveConversionPrimaryAction({
        queueItemCount: 1,
        canStartConversion: true,
        canRunQueue: true,
      }),
    ).toMatchObject({
      primary: "start_conversion",
      showRunQueue: false,
      showStartConversion: true,
    });
  });

  it("gives queue mode ownership of run queue", () => {
    expect(
      resolveConversionPrimaryAction({
        queueItemCount: 2,
        canStartConversion: true,
        canRunQueue: true,
      }),
    ).toMatchObject({
      primary: "run_queue",
      showRunQueue: true,
      showStartConversion: false,
    });
  });

  it("keeps a selected finished source convertible alongside a runnable queue", () => {
    expect(resolveConversionPrimaryAction({
      queueItemCount: 2,
      selectedFinished: true,
      canStartConversion: true,
      canRunQueue: true,
    })).toMatchObject({
      primary: "run_queue",
      showRunQueue: true,
      showStartConversion: true,
    });
  });

  it("starts the one waiting book, not the selected finished one, as the primary action", () => {
    expect(resolveConversionPrimaryAction({
      queueItemCount: 1,
      selectedFinished: true,
      canStartConversion: true,
      canRunQueue: true,
    })).toMatchObject({
      primary: "run_queue",
      showRunQueue: true,
      showStartConversion: true,
    });
  });

  it("leaves converting again as the primary action while the waiting books cannot start", () => {
    expect(resolveConversionPrimaryAction({
      queueItemCount: 2,
      selectedFinished: true,
      canStartConversion: true,
      canRunQueue: false,
    }).primary).toBe("start_conversion");
    // Nothing waits: the finished book is all there is to start.
    expect(resolveConversionPrimaryAction({
      queueItemCount: 0,
      selectedFinished: true,
      canStartConversion: true,
      canRunQueue: false,
    })).toMatchObject({
      primary: "start_conversion",
      showRunQueue: false,
      showStartConversion: true,
    });
  });
});

import { expect, it } from "vitest";

import { withRequestSlot } from "./coverArt";

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

it("never runs more than four cover requests, even when a caller arrives during a hand-off", async () => {
  let running = 0;
  let peak = 0;
  const gates: Array<() => void> = [];
  const request = () => withRequestSlot(async () => {
    running += 1;
    peak = Math.max(peak, running);
    await new Promise<void>((resolve) => gates.push(resolve));
    running -= 1;
  });

  const requests = Array.from({ length: 5 }, request);
  await settle();
  expect(running).toBe(4);

  // Finish one request; a new caller lands in the microtask between the
  // released slot and the queued request taking it over.
  gates.shift()!();
  void Promise.resolve().then(() => undefined).then(() => requests.push(request()));
  await settle();
  expect(peak).toBe(4);
  expect(running).toBe(4);

  while (gates.length) {
    gates.shift()!();
    await settle();
  }
  await Promise.all(requests);
  expect(running).toBe(0);
});

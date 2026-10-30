import test from "node:test";
import assert from "node:assert/strict";
import { waitForScreenConversion } from "../screens.mjs";

const snapshot = (status, id = "seed-job") => ({ active: status === "running", job: { id, status } });

test("screen seeding waits for the submitted job to complete", async () => {
  const states = [snapshot("running"), snapshot("completed")];
  const calls = [];
  const result = await waitForScreenConversion(async (...args) => {
    calls.push(args);
    return states.shift();
  }, "seed-job", { intervalMs: 0 });
  assert.equal(result.job.status, "completed");
  assert.deepEqual(calls, [["GET", "/api/conversions/active"], ["GET", "/api/conversions/active"]]);
});

for (const state of [snapshot("failed"), snapshot("cancelled"), snapshot("idle"), { active: false, job: null }, snapshot("completed", "another-job")]) {
  test(`screen seeding rejects ${state.job?.status ?? "missing"} outcome for ${state.job?.id ?? "no job"}`, async () => {
    await assert.rejects(waitForScreenConversion(async () => state, "seed-job"), /did not complete as expected|Expected conversion/);
  });
}

test("screen capture cleanup can explicitly accept its cancelled demo job", async () => {
  const result = await waitForScreenConversion(async () => snapshot("cancelled"), "seed-job", {
    allowedStatuses: ["completed", "cancelled"],
  });
  assert.equal(result.job.status, "cancelled");
});

test("screen seeding rejects a missing start identity before polling", async () => {
  let polled = false;
  await assert.rejects(waitForScreenConversion(async () => { polled = true; }, undefined), /did not return a job id/);
  assert.equal(polled, false);
});

test("screen seeding fails when the job remains active beyond its budget", async () => {
  await assert.rejects(waitForScreenConversion(async () => snapshot("running"), "seed-job", {
    timeoutMs: 2, intervalMs: 2,
  }), /did not finish/);
});

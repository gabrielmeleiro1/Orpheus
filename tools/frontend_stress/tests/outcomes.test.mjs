import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import * as outcomes from "../lib/util.mjs";

test("redaction removes raw and encoded credentials from diagnostic errors", () => {
  assert.equal(outcomes.redactSecrets("token=abc%2B123 Bearer abc+123", ["abc+123"]), "token=[redacted] Bearer [redacted]");
});

test('natural playback reaching four seconds is not restored playback', () => {
  assert.equal(outcomes.hasRestoredPlaybackOffset({ currentTime: 4, playingFor: 4 }), false);
});
test('restored four-second offset remains distinguishable after playback advances', () => {
  assert.equal(outcomes.hasRestoredPlaybackOffset({ currentTime: 4.1, playingFor: 0.1 }), true);
});

test("stopping a child that already died from a signal returns at once", async () => {
  const proc = spawn(process.execPath, ["-e", "process.kill(process.pid, 'SIGKILL')"], { stdio: "ignore" });
  await once(proc, "exit");
  assert.equal(proc.exitCode, null);
  const stopped = await Promise.race([
    outcomes.makeStopFn(proc)().then(() => "stopped"),
    new Promise((resolve) => setTimeout(resolve, 2000, "hung")).then((value) => value),
  ]);
  assert.equal(stopped, "stopped");
});

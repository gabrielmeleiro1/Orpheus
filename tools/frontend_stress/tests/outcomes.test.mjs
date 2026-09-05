import test from "node:test";
import assert from "node:assert/strict";
import * as outcomes from "../lib/util.mjs";

for (const status of ["failed", "cancelled", "running", undefined]) {
  test(`conversion ${status} cannot be reported as success or unavailable`, () => {
    assert.throws(() => outcomes.requireSuccessfulConversion(status), (error) =>
      error.isSkip !== true && /Conversion did not complete/.test(error.message));
  });
}
test("completed conversion is accepted", () => {
  assert.doesNotThrow(() => outcomes.requireSuccessfulConversion("completed"));
});
test("redaction removes raw and encoded credentials from diagnostic errors", () => {
  assert.equal(outcomes.redactSecrets("token=abc%2B123 Bearer abc+123", ["abc+123"]), "token=[redacted] Bearer [redacted]");
});

test('natural playback reaching four seconds is not restored playback', () => {
  assert.equal(outcomes.hasRestoredPlaybackOffset({ currentTime: 4, playingFor: 4 }), false);
});
test('restored four-second offset remains distinguishable after playback advances', () => {
  assert.equal(outcomes.hasRestoredPlaybackOffset({ currentTime: 4.1, playingFor: 0.1 }), true);
});

import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

for (const argument of ["--backend-url", "--token=", "--frontend-url=  ", "--state-dir", "--only", "--frontend-port=0", "--typo"]) {
  test(`invalid ${argument.split("=")[0]} fails before starting services`, () => {
    const result = spawnSync(process.execPath, [fileURLToPath(new URL("../run.mjs", import.meta.url)), argument], {
      encoding: "utf8", timeout: 15_000,
    });
    assert.equal(result.status, 1, result.error?.message ?? result.stderr);
    assert.match(result.stderr, /requires a value|must be an integer|unrecognized argument/);
    assert.doesNotMatch(result.stdout, /spawning backend|self-start mode/);
  });
}

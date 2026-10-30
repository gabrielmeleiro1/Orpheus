import test from "node:test";
import assert from "node:assert/strict";
import { selectScenarios } from "../lib/scenarioSelection.mjs";

const scenarios = [
  { id: "a", name: "connect" },
  { id: "e", name: "convert-single" },
  { id: "l", name: "cuda-cleaner", optIn: true },
];

test("default selection excludes opt-in hardware checks", () => {
  assert.deepEqual(selectScenarios(scenarios, null), scenarios.slice(0, 2));
});
test("explicit IDs and names preserve execution order without duplicate runs", () => {
  assert.deepEqual(selectScenarios(scenarios, ["cuda-cleaner", "a", "connect"]), [scenarios[0], scenarios[2]]);
});
test("empty and partially misspelled selections cannot silently pass", () => {
  for (const only of [[], ["typo"], ["a", "convert-sigle"]]) {
    assert.throws(() => selectScenarios(scenarios, only), /scenario/);
  }
});

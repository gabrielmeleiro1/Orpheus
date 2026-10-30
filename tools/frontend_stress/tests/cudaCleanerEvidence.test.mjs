import test from "node:test";
import assert from "node:assert/strict";

import { requireCudaCleanerTrace } from "../scenarios.mjs";

const original = "Iam tired. Ibegin again. Iwish the wall would stop moving.";
const corrected = "I am tired. I begin again. I wish the wall would stop moving.";
const successfulTrace = () => ({
  original_text: original,
  cleaned_text: corrected,
  mode: "llm_success",
  llm_error: null,
});

test("saved LLM success with corrected fixture prose is accepted", () => {
  const trace = successfulTrace();
  assert.equal(requireCudaCleanerTrace([trace]), trace);
});

test("missing saved traces report the backend debug prerequisite instead of an undefined mode", () => {
  for (const traces of [[], undefined, null]) {
    assert.throws(() => requireCudaCleanerTrace(traces), (error) => {
      assert.match(error.message, /No saved CUDA cleaning traces.*SAVE_CLEANING_DEBUG=1/);
      assert.notEqual(error.isSkip, true);
      return true;
    });
  }
});

test("corrected text alone cannot substitute for the fixture's inference trace", () => {
  assert.throws(() => requireCudaCleanerTrace([
    { ...successfulTrace(), original_text: "Unrelated source" },
    { cleaned_text: corrected },
  ]), /No saved cleaning trace matches the CUDA source fixture/);
});

for (const mode of ["rule_only", "llm_fallback", "hallucination_fallback", undefined]) {
  test(`${mode} cannot pass even when the saved prose is correct`, () => {
    assert.throws(() => requireCudaCleanerTrace([{ ...successfulTrace(), mode }]),
      /expected actual CUDA inference/);
  });
}

test("a successful unrelated chunk cannot hide fallback for the CUDA fixture", () => {
  assert.throws(() => requireCudaCleanerTrace([
    { ...successfulTrace(), original_text: "Unrelated source" },
    { ...successfulTrace(), mode: "llm_fallback", llm_error: "model unavailable" },
  ]), /expected actual CUDA inference, got llm_fallback/);
});

test("success mode still fails when a cleaner error is recorded", () => {
  assert.throws(() => requireCudaCleanerTrace([{ ...successfulTrace(), llm_error: "model unavailable" }]),
    /must not silently fall back/);
});

test("inference must save the required correction without rewritten prose", () => {
  for (const cleaned_text of [original, "I am tired. I begin anew. I wish the wall would stop moving.", undefined]) {
    assert.throws(() => requireCudaCleanerTrace([{ ...successfulTrace(), cleaned_text }]),
      /must repair the extraction artifacts without rewriting prose/);
  }
});

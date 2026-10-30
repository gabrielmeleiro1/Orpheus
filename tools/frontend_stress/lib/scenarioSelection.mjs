/** Reject misspelled selections before starting services or reporting a green run. */
export function selectScenarios(scenarios, only) {
  if (only === null) return scenarios.filter((scenario) => !scenario.optIn);
  if (!only.length) throw new Error("--only must select at least one scenario");
  const unknown = only.filter((name) => !scenarios.some((scenario) => scenario.id === name || scenario.name === name));
  if (unknown.length) throw new Error(`Unknown scenario selection: ${unknown.join(", ")}`);
  return scenarios.filter((scenario) => only.includes(scenario.id) || only.includes(scenario.name));
}

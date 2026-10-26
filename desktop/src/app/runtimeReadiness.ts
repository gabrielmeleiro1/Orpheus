import type { ConversionRuntimeState } from "../features/conversion/conversionPanelStateTypes";
import type { RuntimeSetupStatusResponse } from "../types/api";

/** The engine catalog describes support, not whether packages and weights are usable. */
export function runtimeTtsReady(status: RuntimeSetupStatusResponse | null): boolean | null {
  if (!status) return null;
  if (status.restart_required || status.state === "restart_required") return false;
  const ttsGroup = (id: string) => id === "tts-mlx" || id === "tts-cpu-cuda";
  const configuredReady = status.configured && status.state === "ready"
    && status.ready && status.packages.ready && status.probe.ready
    && status.selected_model_groups.some((group) =>
      ttsGroup(group.id) && group.ready && group.assets_ready && group.runtime_ready);
  // A library-only selection can coexist with a verified bundled runtime.
  const installed = status.installed_runtime;
  const installedReady = installed?.ready && installed.runtime_available
    && installed.model_groups.some((group) =>
      ttsGroup(group.id) && group.ready && group.assets_ready && group.runtime_ready);
  return Boolean(configuredReady || installedReady);
}

/**
 * What Create may do with this status. `checking` is a configured runtime
 * still being verified; `checkFailed` is a status read that gave up, which
 * (unlike one still in flight) will not turn ready on its own.
 */
export function conversionRuntimeState(
  status: RuntimeSetupStatusResponse | null,
  options: { checking: boolean; checkFailed: boolean },
): ConversionRuntimeState {
  if (!status) return options.checkFailed ? "unavailable" : "checking";
  if (options.checking) return "checking";
  return runtimeTtsReady(status) === true ? "ready" : "unavailable";
}

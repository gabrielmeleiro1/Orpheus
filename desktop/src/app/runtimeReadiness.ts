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

// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { conversionPanelViewModel } from "../../../test/conversionPanelViewModel";
import type { SystemCapabilitiesResponse } from "../../../types/api";
import { createLiveCapabilities } from "../liveCapabilities";
import { AdvancedOptionsView } from "./AdvancedOptionsView";

const GIB = 2 ** 30;

function snapshot(availableGib: number): SystemCapabilitiesResponse {
  return {
    default_tts_engine: "kokoro",
    default_tts_quantization: "",
    cleaner_runtime_availability: {},
    runtime_probe: { state: "pending", refresh_in_progress: true },
    parallel_worker_recommendation: {
      memory_measurement_available: true, total_memory_bytes: 32 * GIB, available_memory_bytes: availableGib * GIB,
      reserved_memory_bytes: 4 * GIB, headroom_memory_bytes: (availableGib - 4) * GIB, static_estimated_worker_bytes: 2 * GIB,
      warmup_memory_delta_bytes: null, estimated_per_worker_memory_bytes: 2 * GIB, requested_parallel_chapters: 4,
      max_safe_parallel_chapters: 4, recommended_parallel_chapters: 4, reason: "static estimate with live headroom",
      calculation: `available - reserved = ${availableGib} GiB - 4 GiB`,
    },
  };
}

describe("AdvancedOptionsView worker status", () => {
  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("follows live memory readings without the panel re-rendering", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    const liveCapabilities = createLiveCapabilities();
    const { advancedOptions, actions } = conversionPanelViewModel();
    let parentRenders = 0;
    function Panel() {
      parentRenders += 1;
      return (
        <AdvancedOptionsView
          advancedOptions={{ ...advancedOptions, selectedParallelWorkerLimit: 2,
            parallelWorkerStatus: "Recommended: 4 · 12.0 GiB memory free", liveCapabilities }}
          actions={actions}
        />
      );
    }
    const status = () => container.querySelector("#parallel-worker-status");
    try {
      await act(async () => root.render(<Panel />));
      // Until a snapshot arrives, the view model's line is shown.
      expect(status()?.textContent).toBe("Recommended: 4 · 12.0 GiB memory free");

      await act(async () => liveCapabilities.publish(snapshot(10)));
      expect(status()?.textContent).toBe("Recommended: 4 · 10.0 GiB memory free");
      expect(status()?.getAttribute("data-tip")).toBe("available - reserved = 10 GiB - 4 GiB");
      await act(async () => liveCapabilities.publish(snapshot(9.5)));
      expect(status()?.textContent).toBe("Recommended: 4 · 9.5 GiB memory free");
      expect(parentRenders).toBe(1);
    } finally {
      await act(async () => root.unmount());
      container.remove();
    }
  });
});

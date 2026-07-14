import { renderToString } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "../../api/client";
import type { RuntimeSetupStatusResponse } from "../../types/api";
import { SetupPage } from "./SetupPage";

const status: RuntimeSetupStatusResponse = {
  schema_version: 1,
  configured: false,
  state: "unconfigured",
  state_error: null,
  selected_profile: "mlx",
  selected_model_groups: [{ id: "tts-mlx", assets_ready: false, assets_reason: "missing", runtime_ready: false, ready: false }],
  packages: { ready: false, missing: [], incompatible: [] },
  probe: { ready: false, checks: [] },
  ready: false,
  restart_required: false,
  profiles: [
    { id: "base", label: "Library only", description: "Documents and playback.", compatible: true, reason: null, model_groups: [], risk: null },
    { id: "mlx", label: "Apple Silicon (MLX)", description: "Private local models.", compatible: true, reason: null, model_groups: ["tts-mlx", "cleaner-mlx"], risk: null },
    { id: "cuda", label: "NVIDIA CUDA", description: "Local CUDA models.", compatible: false, reason: "Linux or Windows only.", model_groups: ["tts-cpu-cuda"], risk: { id: "pytorch", summary: "Use trusted models only.", accepted: false } },
  ],
  risk: null,
  backend: { instance_id: "backend-1", status: "running" },
};

describe("SetupPage", () => {
  it("matches the Orpheus shell and renders accessible runtime/model choices", () => {
    const html = renderToString(
      <SetupPage
        apiClient={{ getRuntimeSetupStatus: vi.fn() } as unknown as ApiClient}
        initialStatus={status}
        initialError={null}
        onReady={vi.fn()}
        onContinueLibraryOnly={vi.fn()}
        onReconnectBackend={vi.fn()}
      />,
    );
    expect(html).toContain("orpheus.local");
    expect(html).toContain("Runtime setup");
    expect(html).toContain('type="radio"');
    expect(html).toContain("Apple Silicon (MLX)");
    expect(html).toContain("Kokoro TTS · MLX");
    expect(html).toContain("Linux or Windows only.");
    expect(html).toContain("Estimates");
  });
});

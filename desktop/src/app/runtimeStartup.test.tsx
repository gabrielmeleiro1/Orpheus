// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { beforeEach, expect, it, vi } from "vitest";

import type { RuntimeSetupStatusResponse } from "../types/api";
import type { ConversionJobController } from "../features/conversion/useConversionJob";
import { readReadyRuntimeHint, rememberReadyRuntime, showDashboardDuringRuntimeCheck } from "./runtimeStartup";
import { useVerifiedConversionJob } from "./useVerifiedConversionJob";

const ready = {
  configured: true, ready: true, state: "ready", selected_profile: "cuda", restart_required: false,
  selected_model_groups: [], probe: { ready: true, checks: [] },
} as unknown as RuntimeSetupStatusResponse;
const pending = { ...ready, ready: false, state: "checking" as const };

beforeEach(() => {
  localStorage.clear();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

it("opens a returning dashboard through pending checks without restoring readiness", () => {
  expect(readReadyRuntimeHint()).toBe(false);
  expect(showDashboardDuringRuntimeCheck(false, null, null)).toBe(false);
  rememberReadyRuntime(ready);
  expect(readReadyRuntimeHint()).toBe(true);
  expect(showDashboardDuringRuntimeCheck(readReadyRuntimeHint(), null, null)).toBe(true);
  rememberReadyRuntime(pending);
  expect(showDashboardDuringRuntimeCheck(readReadyRuntimeHint(), pending, null)).toBe(true);
  expect(pending.ready).toBe(false);
  expect(showDashboardDuringRuntimeCheck(true, ready, null)).toBe(false);
});

it.each([
  { ...ready, configured: false, ready: false, state: "unconfigured" as const },
  { ...ready, ready: false, state: "unavailable" as const },
  { ...ready, restart_required: true, state: "restart_required" as const },
])("drops the hint when the current runtime is definitively unavailable", (status) => {
  rememberReadyRuntime(ready);
  rememberReadyRuntime(status);
  expect(readReadyRuntimeHint()).toBe(false);
  expect(showDashboardDuringRuntimeCheck(true, status, null)).toBe(false);
});

it("keeps connection errors visible even with a saved hint", () => {
  expect(showDashboardDuringRuntimeCheck(true, null, "backend unavailable")).toBe(false);
});

it("blocks all submissions until verified and revokes even a previously captured start callback", async () => {
  const submit = vi.fn(async () => ({ ok: true as const, jobId: "job" }));
  const job = { startConversion: submit } as unknown as ConversionJobController;
  let guarded!: ConversionJobController;
  function Probe({ verified }: { verified: boolean }) {
    guarded = useVerifiedConversionJob(job, verified);
    return null;
  }
  const container = document.createElement("div");
  const root = createRoot(container);
  const payload = { source_path: "/book.txt", conversion_mode: "audiobook" as const };
  try {
    await act(async () => root.render(<Probe verified={false} />));
    const start = guarded.startConversion;
    expect(await start(payload)).toMatchObject({ ok: false, status: 409, serverCode: "runtime_check_pending" });
    expect(submit).not.toHaveBeenCalled();
    await act(async () => root.render(<Probe verified />));
    expect(await start(payload)).toEqual({ ok: true, jobId: "job" });
    expect(submit).toHaveBeenCalledExactlyOnceWith(payload);
    await act(async () => root.render(<Probe verified={false} />));
    expect(await start(payload)).toMatchObject({ ok: false, status: 409 });
    expect(submit).toHaveBeenCalledTimes(1);
  } finally {
    await act(async () => root.unmount());
  }
});

import { expect, it, vi } from "vitest";

import type { RequestJson } from "../http";
import { createMaintenanceApi } from "./maintenance";

it("keeps folders no book owns unless a cleanup names them", async () => {
  const requestJson = vi.fn(async () => ({}));
  const api = createMaintenanceApi(requestJson as unknown as RequestJson);

  await api.cleanupStorage();
  await api.cleanupStorage({ removeUnownedFolders: [] });
  await api.cleanupStorage({ removeUnownedFolders: ["Old Book"], timeoutMs: 5 });

  expect(requestJson.mock.calls).toEqual([
    ["/api/maintenance/cleanup", { method: "POST", body: JSON.stringify({ confirm: "cleanup" }) }],
    ["/api/maintenance/cleanup", { method: "POST", body: JSON.stringify({ confirm: "cleanup" }) }],
    ["/api/maintenance/cleanup", {
      method: "POST", body: JSON.stringify({ confirm: "cleanup", remove_unowned_folders: ["Old Book"] }), timeoutMs: 5,
    }],
  ]);
});

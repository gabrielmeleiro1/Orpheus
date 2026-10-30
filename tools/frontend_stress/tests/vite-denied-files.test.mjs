import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createServer } from "../../../desktop/node_modules/vite/dist/node/index.js";

test("Vite denies certificate files through every transform query", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "orpheus-vite-deny-"));
  let server;
  try {
    await fs.writeFile(path.join(root, "synthetic.crt"), "Public synthetic fixture; no credential.");
    server = await createServer({
      configFile: false, root, logLevel: "silent",
      server: { host: "127.0.0.1", port: 0 },
    });
    await server.listen();
    const address = server.httpServer.address();
    for (const query of ["?raw", "?import&raw", "?import&url&inline"]) {
      const response = await fetch(`http://127.0.0.1:${address.port}/synthetic.crt${query}`);
      await response.arrayBuffer();
      assert.equal(response.status, 403, `Denied file was exposed through ${query}`);
    }
  } finally {
    await server?.close();
    await fs.rm(root, { recursive: true, force: true });
  }
});

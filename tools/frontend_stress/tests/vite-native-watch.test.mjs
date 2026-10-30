import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { createServer } from "../../../desktop/node_modules/vite/dist/node/index.js";

test("Vite does not watch staged native binaries", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "orpheus-native-watch-"));
  const binaries = path.join(root, "src-tauri", "binaries", ".backend-staging");
  let server;
  try {
    await fs.mkdir(binaries, { recursive: true });
    await fs.writeFile(path.join(binaries, "synthetic.exe"), "Public watcher fixture.");
    server = await createServer({
      configFile: fileURLToPath(new URL("../../../desktop/vite.config.ts", import.meta.url)),
      root, logLevel: "silent", server: { host: "127.0.0.1", port: 0 },
    });
    const ready = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error("Watcher did not become ready")), 10000);
      server.watcher.once("ready", () => { clearTimeout(timeout); resolve(); });
      server.watcher.once("error", error => { clearTimeout(timeout); reject(error); });
    });
    await server.listen();
    await ready;
    const deadline = Date.now() + 5000;
    while (!Object.keys(server.watcher.getWatched()).some(directory => path.resolve(directory) === root)) {
      assert.ok(Date.now() < deadline, `Root watcher did not start: ${server.config.root}`);
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    await new Promise(resolve => setTimeout(resolve, 100));
    const watched = Object.keys(server.watcher.getWatched());
    assert.equal(watched.some(directory => path.resolve(directory) === binaries), false,
      "Generated native binaries must be excluded from Vite's file watcher");
  } finally {
    await server?.close();
    await fs.rm(root, { recursive: true, force: true });
  }
});

#!/usr/bin/env node
// Runs the desktop app's Vite dev server programmatically instead of via the
// `vite` CLI binary.
//
// Why: this worktree's `desktop/node_modules` is a symlink to the main
// checkout's `desktop/node_modules` (outside this worktree). Vite's default
// `server.fs.strict` only serves files that resolve under the project root,
// so any package asset resolved through that symlink (e.g. the
// @fontsource-variable/geist .woff2 files loaded via `/@fs/...`) comes back
// 403 Forbidden. That 403 surfaces as a real browser console error, which
// would fail every scenario under this harness's console-error policy.
//
// Loading Vite's JS API directly (resolved from the desktop directory via
// createRequire, since this script's own package.json only depends on
// playwright) lets us pass `server.fs.strict: false` as an override on top
// of the project's real vite.config.ts, without editing any file outside
// tools/frontend_stress/.
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

async function main() {
  const [desktopDir, portArg] = process.argv.slice(2);
  const port = Number(portArg);

  const requireFromDesktop = createRequire(path.join(desktopDir, "package.json"));
  const vitePath = requireFromDesktop.resolve("vite");
  const { createServer } = await import(pathToFileURL(vitePath).href);

  const server = await createServer({
    root: desktopDir,
    configFile: path.join(desktopDir, "vite.config.ts"),
    server: {
      host: "127.0.0.1",
      port,
      strictPort: true,
      fs: { strict: false },
    },
  });

  await server.listen();
  process.send?.({ event: "listening", port, pid: process.pid });

  const shutdown = async () => {
    try {
      await server.close();
    } finally {
      process.exit(0);
    }
  };
  process.on("SIGTERM", shutdown);
  process.on("SIGINT", shutdown);
}

main().catch((err) => {
  console.error("vite-server-child failed:", err);
  process.exit(1);
});

#!/usr/bin/env node
// Runs the desktop app's Vite dev server programmatically instead of via the
// `vite` CLI binary.
//
// Why: `desktop/node_modules` may be a symlink (a git worktree sharing another
// checkout's install). Package assets such as the @fontsource .woff2 files then
// resolve outside the project root, Vite's `server.fs.strict` answers 403, and
// the browser console error fails every scenario. Vite's JS API (resolved from
// the desktop directory, since this package only depends on playwright) lets
// the harness add that one resolved directory to Vite's default allow list on
// top of the project's vite.config.ts; everything else on disk stays denied.
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { pathToFileURL } from "node:url";

async function main() {
  const [desktopDir, portArg] = process.argv.slice(2);
  const port = Number(portArg);

  const requireFromDesktop = createRequire(path.join(desktopDir, "package.json"));
  const vitePath = requireFromDesktop.resolve("vite");
  const { createServer, searchForWorkspaceRoot } = await import(pathToFileURL(vitePath).href);
  const nodeModules = fs.realpathSync(path.join(desktopDir, "node_modules"));

  const server = await createServer({
    root: desktopDir,
    configFile: path.join(desktopDir, "vite.config.ts"),
    server: {
      host: "127.0.0.1",
      port,
      strictPort: true,
      fs: { allow: [searchForWorkspaceRoot(desktopDir), nodeModules] },
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

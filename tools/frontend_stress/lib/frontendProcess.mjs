import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const VITE_SERVER_CHILD = path.join(__dirname, "viteServerChild.mjs");

/**
 * Spawns the Vite dev server for the desktop React app via a small
 * programmatic child (lib/viteServerChild.mjs) rather than the `vite` CLI
 * binary, so we can force `server.fs.strict: false`. This worktree's
 * `desktop/node_modules` is a symlink to the main checkout's node_modules,
 * so package assets (e.g. @fontsource woff2 files) resolve to real paths
 * outside the project root and Vite's default fs.strict would 403 them.
 */
export async function startFrontend({ desktopDir, port, log, readyTimeoutMs = 30_000 }) {
  if (!fs.existsSync(path.join(desktopDir, "node_modules"))) {
    throw new Error(`desktop/node_modules not found under ${desktopDir}; run "npm install" in desktop/ first`);
  }

  log(`spawning frontend: node ${VITE_SERVER_CHILD} ${desktopDir} ${port}`);
  const proc = spawn(process.execPath, [VITE_SERVER_CHILD, desktopDir, String(port)], {
    cwd: desktopDir,
    env: { ...process.env },
    stdio: ["ignore", "pipe", "pipe"],
  });

  const outputChunks = [];
  proc.stdout.on("data", (chunk) => outputChunks.push(chunk.toString()));
  proc.stderr.on("data", (chunk) => outputChunks.push(chunk.toString()));

  // The desktop backend's CORS allowlist (modules/web/server.py,
  // DESKTOP_DEV_ORIGIN) only accepts the exact origin "http://localhost:5173"
  // — not "http://127.0.0.1:5173" and not any other port — so the frontend
  // must be reachable at that precise origin for API calls to succeed.
  const baseUrl = `http://localhost:${port}`;
  const deadline = Date.now() + readyTimeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) {
      throw new Error(`vite exited early (code=${proc.exitCode}):\n${outputChunks.join("")}`);
    }
    try {
      const response = await fetch(baseUrl, { method: "GET" });
      if (response.ok || response.status === 304) {
        log(`frontend ready at ${baseUrl}`);
        return { proc, baseUrl, stop: makeStopFn(proc), getOutput: () => outputChunks.join("") };
      }
    } catch (err) {
      lastError = err;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }

  throw new Error(
    `frontend did not become ready within ${readyTimeoutMs}ms (last error: ${lastError}). output:\n${outputChunks.join("")}`,
  );
}

function makeStopFn(proc) {
  let stopped = false;
  return async () => {
    if (stopped || proc.exitCode !== null || proc.killed) {
      stopped = true;
      return;
    }
    stopped = true;
    await new Promise((resolve) => {
      proc.once("exit", () => resolve());
      proc.kill("SIGTERM");
      setTimeout(() => {
        if (proc.exitCode === null) {
          proc.kill("SIGKILL");
        }
      }, 5_000);
    });
  };
}

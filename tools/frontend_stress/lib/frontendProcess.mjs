import { spawn } from "node:child_process";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

import { registerCleanup } from "./cleanupRegistry.mjs";

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
    stdio: ["ignore", "pipe", "pipe", "ipc"],
  });

  // Defined and registered immediately (before the readiness poll below) so
  // this process is guaranteed to get torn down even if it never becomes
  // ready — whether via the timeout path below, an interrupt, or a later
  // stage of the harness failing first.
  const stop = makeStopFn(proc);
  registerCleanup(stop);

  const outputChunks = [];
  proc.stdout.on("data", (chunk) => outputChunks.push(chunk.toString()));
  proc.stderr.on("data", (chunk) => outputChunks.push(chunk.toString()));

  // The desktop backend's CORS allowlist (modules/web/server.py,
  // DESKTOP_DEV_ORIGIN) only accepts the exact origin "http://localhost:5173"
  // — not "http://127.0.0.1:5173" and not any other port — so the frontend
  // must be reachable at that precise origin for API calls to succeed.
  const baseUrl = `http://localhost:${port}`;
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`frontend did not become ready within ${readyTimeoutMs}ms`)), readyTimeoutMs);
      const finish = (error) => {
        clearTimeout(timer);
        error ? reject(error) : resolve();
      };
      proc.once("error", finish);
      proc.once("exit", (code) => finish(new Error(`vite exited early (code=${code}): ${outputChunks.join("")}`)));
      proc.on("message", (message) => {
        if (message?.event === "listening" && message.port === port && message.pid === proc.pid) finish();
      });
    });
    // Probe the same IPv4 listener the authenticated child just opened.
    const response = await fetch(`http://127.0.0.1:${port}`, { signal: AbortSignal.timeout(5_000) });
    if (!response.ok) throw new Error(`frontend readiness probe returned HTTP ${response.status}`);
    log(`frontend ready at ${baseUrl}`);
    return { proc, baseUrl, stop, getOutput: () => outputChunks.join("") };
  } catch (error) {
    await stop();
    throw error;
  }
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

import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import fs from "node:fs";
import path from "node:path";

import { registerCleanup } from "./cleanupRegistry.mjs";
import { makeStopFn } from "./util.mjs";

/**
 * The checkout backend by default. ORPHEUS_HARNESS_BACKEND_EXE runs a packaged
 * sidecar (orpheus-desktop-backend.exe) instead, so a journey can prove what
 * ships rather than the source tree. The sidecar starts in the isolated state
 * folder: if an environment override were lost it would write there, not
 * beside the build.
 */
export function resolveBackendCommand({ repoRoot, stateDir, environment = process.env }) {
  const listen = ["--host", "127.0.0.1", "--port", "0"];
  const packaged = String(environment.ORPHEUS_HARNESS_BACKEND_EXE ?? "").trim();
  if (packaged) {
    const command = path.resolve(packaged);
    if (!fs.existsSync(command)) throw new Error(`ORPHEUS_HARNESS_BACKEND_EXE does not exist: ${command}`);
    return { command, args: listen, cwd: stateDir };
  }
  const python = path.join(repoRoot, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  if (!fs.existsSync(python)) throw new Error("Checkout .venv Python is missing; follow the repository setup instructions first.");
  return { command: python, args: ["-m", "apps.launchers.desktop_backend", ...listen], cwd: repoRoot };
}

/**
 * Spawns the Orpheus desktop backend (FastAPI/uvicorn) with isolated state
 * directories and parses its single-line JSON "ready" event from stdout.
 */
export async function startBackend({ repoRoot, stateDir, log, readyTimeoutMs = 60_000 }) {
  const dataDir = path.join(stateDir, "data");
  const outputDir = path.join(stateDir, "output");
  const tempDir = path.join(stateDir, "temp");
  const dbPath = path.join(dataDir, "audiobooks.db");
  for (const dir of [dataDir, outputDir, tempDir]) {
    fs.mkdirSync(dir, { recursive: true });
  }

  const env = {
    ...process.env,
    ORPHEUS_DESKTOP_DEV: "1",
    // Keep cleaning traces in the isolated state so cleaner journeys can prove
    // which backend actually cleaned each chunk.
    SAVE_CLEANING_DEBUG: process.env.SAVE_CLEANING_DEBUG ?? "1",
    HF_HUB_OFFLINE: "1",
    TRANSFORMERS_OFFLINE: "1",
    AUDIOBOOK_DATA_DIR: dataDir,
    AUDIOBOOK_OUTPUT_DIR: outputDir,
    AUDIOBOOK_TEMP_DIR: tempDir,
    AUDIOBOOK_DB_PATH: dbPath,
  };

  const { command, args, cwd } = resolveBackendCommand({ repoRoot, stateDir });
  log(`spawning backend: ${command} ${args.join(" ")} (cwd=${cwd})`);
  const proc = spawn(command, args, { cwd, env, stdio: ["ignore", "pipe", "pipe"] });

  // Defined before the readiness wait (and registered immediately) so the
  // process is guaranteed to get torn down even if it never becomes ready —
  // whether via the timeout/error paths below, an interrupt, or a later
  // stage of the harness failing first.
  const stop = makeStopFn(proc);
  registerCleanup(stop);

  const stderrChunks = [];
  proc.stderr.on("data", (chunk) => {
    stderrChunks.push(chunk.toString());
  });

  const ready = await new Promise((resolve, reject) => {
    let settled = false;
    const rl = createInterface({ input: proc.stdout });

    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      rl.close();
      stop().catch(() => {});
      reject(
        new Error(
          `backend did not emit ready event within ${readyTimeoutMs}ms. stderr:\n${stderrChunks.join("")}`,
        ),
      );
    }, readyTimeoutMs);

    rl.on("line", (line) => {
      const trimmed = line.trim();
      if (!trimmed.startsWith("{")) {
        return;
      }
      try {
        const payload = JSON.parse(trimmed);
        if (payload.event === "ready" && payload.port) {
          settled = true;
          clearTimeout(timer);
          rl.close();
          // Closing readline pauses stdout; keep draining it so a chatty
          // backend never blocks on a full pipe.
          proc.stdout.resume();
          resolve(payload);
        }
      } catch {
        // ignore non-JSON stdout noise
      }
    });

    proc.on("exit", (code, signal) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(
        new Error(
          `backend process exited early (code=${code}, signal=${signal}). stderr:\n${stderrChunks.join("")}`,
        ),
      );
    });

    proc.on("error", (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      stop().catch(() => {});
      reject(err);
    });
  });

  const baseUrl = `http://${ready.host}:${ready.port}`;
  log(`backend ready at ${baseUrl}`);

  return {
    proc,
    stateDir,
    repoRoot,
    baseUrl,
    host: ready.host,
    port: ready.port,
    token: ready.token,
    dataDir,
    outputDir,
    tempDir,
    dbPath,
    stop,
    getStderr: () => stderrChunks.join(""),
  };
}

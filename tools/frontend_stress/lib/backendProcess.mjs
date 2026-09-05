import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import fs from "node:fs";
import path from "node:path";

import { registerCleanup } from "./cleanupRegistry.mjs";

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
    HF_HUB_OFFLINE: "1",
    TRANSFORMERS_OFFLINE: "1",
    AUDIOBOOK_DATA_DIR: dataDir,
    AUDIOBOOK_OUTPUT_DIR: outputDir,
    AUDIOBOOK_TEMP_DIR: tempDir,
    AUDIOBOOK_DB_PATH: dbPath,
  };

  const python = path.join(repoRoot, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  if (!fs.existsSync(python)) throw new Error("Checkout .venv Python is missing; follow the repository setup instructions first.");
  log(`spawning backend: ${python} -m apps.launchers.desktop_backend --host 127.0.0.1 --port 0 (cwd=${repoRoot})`);
  const proc = spawn(
    python,
    ["-m", "apps.launchers.desktop_backend", "--host", "127.0.0.1", "--port", "0"],
    { cwd: repoRoot, env, stdio: ["ignore", "pipe", "pipe"] },
  );

  // Defined before the readiness wait (and registered immediately) so the
  // process is guaranteed to get torn down even if it never becomes ready —
  // whether via the timeout/error paths below, an interrupt, or a later
  // stage of the harness failing first.
  let stopped = false;
  const stop = async () => {
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

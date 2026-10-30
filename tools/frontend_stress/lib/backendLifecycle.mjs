import fs from "node:fs";
import path from "node:path";

import { redactSecrets } from "./util.mjs";

const STDERR_TAIL_BYTES = 16 * 1024;

export class BackendProcessError extends Error {
  constructor(backend, diagnosticPath) {
    const status = backend.exitedAt
      ? `exited with code ${backend.exitCode ?? "null"}${backend.exitCodeHex ? ` (${backend.exitCodeHex})` : ""}, signal ${backend.signal ?? "none"}`
      : `process error: ${backend.processError?.message ?? "unknown"}`;
    super(`Backend ${status} during "${backend.phase}" (pid ${backend.pid ?? "unavailable"}, ${backend.readyAt ? "after readiness" : "before readiness"}). See ${diagnosticPath}.`
      + (backend.stderrTail ? `\nLast backend stderr:\n${backend.stderrTail}` : ""));
    this.name = "BackendProcessError";
    this.backend = backend;
  }
}

/** Observe the entire child lifetime, independently of the one-shot ready handshake. */
export function observeBackendProcess(proc, { artifactDir, executable, args = [] }) {
  fs.mkdirSync(artifactDir, { recursive: true });
  const processPath = path.join(artifactDir, "backend-process.json");
  const exitPath = path.join(artifactDir, "backend-exit.json");
  const startedAt = new Date().toISOString();
  const secrets = [];
  let stderrTail = Buffer.alloc(0);
  let failure = null;
  let resolveFailure;
  let resolveClosed;
  // Resolve with the error instead of rejecting while no operation is awaiting it.
  const failed = new Promise((resolve) => { resolveFailure = resolve; });
  const closed = new Promise((resolve) => { resolveClosed = resolve; });
  const state = {
    pid: proc.pid ?? null,
    executable,
    args: [...args],
    startedAt,
    phase: "backend startup",
    phaseStartedAt: startedAt,
    readyAt: null,
    backendBaseUrl: null,
    cleanupRequestedAt: null,
    expectedExit: false,
    exitedAt: null,
    closedAt: null,
    exitCode: null,
    exitCodeHex: null,
    signal: null,
    processError: null,
  };
  const snapshot = () => JSON.parse(redactSecrets(JSON.stringify({
    ...state, stderrTail: stderrTail.toString("utf8"),
  }), secrets));
  const persist = () => {
    const serialized = JSON.stringify(snapshot(), null, 2) + "\n";
    fs.writeFileSync(processPath, serialized);
    if (state.exitedAt) fs.writeFileSync(exitPath, serialized);
  };
  const fail = () => {
    if (state.expectedExit || failure) return;
    failure = new BackendProcessError(snapshot(), state.exitedAt ? exitPath : processPath);
    resolveFailure(failure);
  };
  proc.stderr?.on("data", (chunk) => {
    stderrTail = Buffer.concat([stderrTail, Buffer.from(chunk)]);
    if (stderrTail.length > STDERR_TAIL_BYTES) stderrTail = Buffer.from(stderrTail.subarray(-STDERR_TAIL_BYTES));
  });
  proc.once("error", (error) => {
    state.processError = { code: error.code ?? null, message: error.message };
    persist();
    fail();
  });
  proc.once("exit", (code, signal) => {
    state.exitedAt = new Date().toISOString();
    state.exitCode = code;
    // Preserve Node's raw value as well as its unsigned Windows status representation.
    state.exitCodeHex = Number.isInteger(code) ? `0x${(code >>> 0).toString(16).padStart(8, "0")}` : null;
    state.signal = signal;
    persist();
    fail();
  });
  proc.once("close", () => {
    state.closedAt = new Date().toISOString();
    // stdout/stderr may finish after exit; retain the final diagnostic tail too.
    persist();
    resolveClosed(snapshot());
  });
  persist();

  return {
    snapshot,
    closed,
    get failure() { return failure; },
    markReady: (ready) => {
      if (ready.token) secrets.push(ready.token);
      state.readyAt = new Date().toISOString();
      state.backendBaseUrl = `http://${ready.host}:${ready.port}`;
      persist();
    },
    setPhase: (phase) => {
      if (failure || state.exitedAt) return;
      state.phase = phase;
      state.phaseStartedAt = new Date().toISOString();
      persist();
    },
    beginCleanup: () => {
      state.cleanupRequestedAt = new Date().toISOString();
      // Cleanup must not relabel an already-observed unexpected exit.
      if (!failure && !state.exitedAt) state.expectedExit = true;
      persist();
    },
    race: async (operation) => {
      if (failure) throw failure;
      const result = await Promise.race([
        Promise.resolve().then(() => {
          if (failure) throw failure;
          return operation();
        }),
        failed.then((error) => { throw error; }),
      ]);
      if (failure) throw failure;
      return result;
    },
  };
}

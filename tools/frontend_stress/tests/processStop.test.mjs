import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { startBackend } from "../lib/backendProcess.mjs";
import { makeStopFn } from "../lib/util.mjs";

function scratch(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "orpheus-process-stop-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function exited(proc, timeoutMs) {
  if (proc.exitCode !== null || proc.signalCode !== null) return Promise.resolve(true);
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(false), timeoutMs);
    proc.once("exit", () => {
      clearTimeout(timer);
      resolve(true);
    });
  });
}

test("concurrent stop callers all wait for the child to exit", async (t) => {
  const proc = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
  t.after(() => proc.kill("SIGKILL"));
  const stop = makeStopFn(proc);

  const first = stop();
  const second = stop();
  await second;

  assert.equal(first, second);
  assert.ok(proc.exitCode !== null || proc.signalCode !== null, "second caller returned before the child exited");
});

test("stopping a child that never spawned resolves without waiting for exit", async () => {
  const proc = spawn(path.join(os.tmpdir(), "orpheus-missing-executable"), [], { stdio: "ignore" });
  proc.on("error", () => {});
  const stop = makeStopFn(proc);

  const outcome = await Promise.race([
    stop().then(() => "stopped"),
    new Promise((resolve) => setTimeout(() => resolve("hung"), 2_000)),
  ]);

  assert.equal(outcome, "stopped");
});

test("backend stdout keeps draining after the ready event", { skip: process.platform === "win32" }, async (t) => {
  const root = scratch(t);
  const exe = path.join(root, "chatty-backend");
  // Far more than a pipe buffer after ready: the child only exits if stdout is read.
  fs.writeFileSync(
    exe,
    [
      "#!/bin/sh",
      "echo '{\"event\":\"ready\",\"host\":\"127.0.0.1\",\"port\":1,\"token\":\"t\"}'",
      "head -c 1048576 /dev/zero | tr '\\0' 'x'",
      "",
    ].join("\n"),
  );
  fs.chmodSync(exe, 0o755);

  const previous = process.env.ORPHEUS_HARNESS_BACKEND_EXE;
  process.env.ORPHEUS_HARNESS_BACKEND_EXE = exe;
  t.after(() => {
    if (previous === undefined) delete process.env.ORPHEUS_HARNESS_BACKEND_EXE;
    else process.env.ORPHEUS_HARNESS_BACKEND_EXE = previous;
  });

  const backend = await startBackend({ repoRoot: root, stateDir: path.join(root, "state"), log: () => {}, readyTimeoutMs: 5_000 });
  t.after(() => backend.stop());

  assert.equal(await exited(backend.proc, 5_000), true, "backend blocked writing to an undrained stdout pipe");
});

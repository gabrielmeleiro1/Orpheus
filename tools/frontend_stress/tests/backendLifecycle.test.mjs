import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { EventEmitter } from "node:events";

import { BackendProcessError, observeBackendProcess } from "../lib/backendLifecycle.mjs";

function artifactDirectory(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "orpheus-backend-lifecycle-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function observe(t, proc) {
  const artifactDir = artifactDirectory(t);
  const monitor = observeBackendProcess(proc, { artifactDir, executable: process.execPath, args: ["fixture"] });
  return { artifactDir, monitor };
}

test("a real backend exit after ready interrupts a pending matrix wait and persists its status", { timeout: 10_000 }, async (t) => {
  const proc = spawn(process.execPath, ["-e", `
    console.log(JSON.stringify({ event: "ready", host: "127.0.0.1", port: 12345, token: "fixture-secret" }));
    setTimeout(() => {
      process.stderr.write("Loading weights: 0/434\\n", () => process.exit(37));
    }, 200);
  `], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  const { artifactDir, monitor } = observe(t, proc);
  const reader = createInterface({ input: proc.stdout });
  try {
    await monitor.race(() => new Promise((resolve) => reader.on("line", (line) => {
      const ready = JSON.parse(line);
      monitor.markReady(ready);
      resolve(ready);
    })));
    monitor.setPhase("Qwen 2.5 3B conversion, 8bit");
    let advanced = false;
    await assert.rejects(monitor.race(() => new Promise(() => {})).then(() => { advanced = true; }), (error) => {
      assert.ok(error instanceof BackendProcessError);
      assert.match(error.message, /code 37 \(0x00000025\).*Qwen 2.5 3B conversion, 8bit/);
      assert.match(error.message, /after readiness/);
      // Evidence exists when the failure is delivered, not just at final cleanup.
      const exit = JSON.parse(fs.readFileSync(path.join(artifactDir, "backend-exit.json"), "utf8"));
      assert.equal(exit.pid, proc.pid);
      assert.equal(exit.exitCode, 37);
      assert.equal(exit.signal, null);
      assert.equal(exit.expectedExit, false);
      assert.equal(exit.phase, "Qwen 2.5 3B conversion, 8bit");
      return true;
    });
    assert.equal(advanced, false);
    await monitor.closed;
    const exit = JSON.parse(fs.readFileSync(path.join(artifactDir, "backend-exit.json"), "utf8"));
    assert.match(exit.stderrTail, /Loading weights: 0\/434/);
    assert.ok(exit.closedAt);
    assert.ok(exit.readyAt);
    assert.ok(exit.exitedAt);
    assert.equal(exit.backendBaseUrl, "http://127.0.0.1:12345");
    monitor.beginCleanup();
    assert.equal(monitor.snapshot().expectedExit, false);
    await assert.rejects(monitor.race(() => { throw new Error("must not run another matrix step"); }), BackendProcessError);
  } finally {
    reader.close();
    await monitor.closed;
  }
});

test("an exit before readiness fails startup without waiting for its timeout", { timeout: 10_000 }, async (t) => {
  const proc = spawn(process.execPath, ["-e", "process.exit(23)"], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  const { monitor } = observe(t, proc);
  await assert.rejects(monitor.race(() => new Promise(() => {})), /code 23.*before readiness/);
  await monitor.closed;
  assert.equal(monitor.snapshot().readyAt, null);
});

test("zero exit after readiness is still an unexpected matrix failure", async (t) => {
  const proc = new EventEmitter();
  const { monitor } = observe(t, proc);
  monitor.markReady({ host: "127.0.0.1", port: 12345 });
  proc.emit("exit", 0, null);
  await assert.rejects(monitor.race(() => true), /code 0.*after readiness/);
});

test("records signal exits and signed Windows status without guessing a cause", async (t) => {
  const first = new EventEmitter();
  const { monitor: signalled } = observe(t, first);
  first.emit("exit", null, "SIGSEGV");
  assert.equal(signalled.snapshot().exitCode, null);
  assert.equal(signalled.snapshot().exitCodeHex, null);
  assert.equal(signalled.snapshot().signal, "SIGSEGV");
  const second = new EventEmitter();
  const { monitor: windows } = observe(t, second);
  second.emit("exit", -1073741819, null);
  assert.equal(windows.snapshot().exitCode, -1073741819);
  assert.equal(windows.snapshot().exitCodeHex, "0xc0000005");
});

test("bounded final stderr is redacted across chunks and updated after exit", async (t) => {
  const proc = new EventEmitter();
  proc.stderr = new EventEmitter();
  const { artifactDir, monitor } = observe(t, proc);
  monitor.markReady({ host: "127.0.0.1", port: 12345, token: "secret+token" });
  proc.stderr.emit("data", Buffer.from("x".repeat(32 * 1024)));
  proc.stderr.emit("data", Buffer.from("\nBearer secret+"));
  proc.stderr.emit("data", Buffer.from("token and secret%2Btoken\n"));
  proc.emit("exit", 1, null);
  proc.stderr.emit("data", Buffer.from("last stderr after exit\n"));
  proc.emit("close", 1, null);
  await monitor.closed;
  const serialized = fs.readFileSync(path.join(artifactDir, "backend-exit.json"), "utf8");
  assert.doesNotMatch(serialized, /secret\+token|secret%2Btoken/);
  const exit = JSON.parse(serialized);
  assert.match(exit.stderrTail, /Bearer \[redacted\] and \[redacted\]/);
  assert.match(exit.stderrTail, /last stderr after exit/);
  assert.ok(Buffer.byteLength(exit.stderrTail) <= 16 * 1024);
});

test("intentional cleanup is recorded and does not become a backend failure", async (t) => {
  const proc = new EventEmitter();
  const { artifactDir, monitor } = observe(t, proc);
  monitor.beginCleanup();
  proc.emit("exit", 0, null);
  proc.emit("close", 0, null);
  await monitor.closed;
  assert.equal(monitor.failure, null);
  assert.equal(JSON.parse(fs.readFileSync(path.join(artifactDir, "backend-exit.json"), "utf8")).expectedExit, true);
});

test("spawn errors fail promptly and are not reported as observed exits", { timeout: 10_000 }, async (t) => {
  const directory = artifactDirectory(t);
  const executable = path.join(directory, "nonexistent-backend");
  const proc = spawn(executable, [], { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
  const monitor = observeBackendProcess(proc, { artifactDir: directory, executable });
  await assert.rejects(monitor.race(() => new Promise(() => {})), BackendProcessError);
  await monitor.closed;
  const snapshot = JSON.parse(fs.readFileSync(path.join(directory, "backend-process.json"), "utf8"));
  assert.equal(snapshot.processError.code, "ENOENT");
  assert.equal(snapshot.exitedAt, null);
  assert.equal(fs.existsSync(path.join(directory, "backend-exit.json")), false);
});

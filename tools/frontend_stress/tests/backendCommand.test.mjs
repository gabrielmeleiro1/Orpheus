import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { resolveBackendCommand } from "../lib/backendProcess.mjs";

function scratch(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "orpheus-backend-command-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test("runs a packaged sidecar from the isolated state folder when one is named", (t) => {
  const root = scratch(t);
  const exe = path.join(root, "orpheus-desktop-backend.exe");
  fs.writeFileSync(exe, "");
  const stateDir = path.join(root, "state");
  const resolved = resolveBackendCommand({ repoRoot: root, stateDir, environment: { ORPHEUS_HARNESS_BACKEND_EXE: ` ${exe} ` } });
  assert.deepEqual(resolved, { command: exe, args: ["--host", "127.0.0.1", "--port", "0"], cwd: stateDir });
});

test("refuses a packaged sidecar path that does not exist", (t) => {
  const root = scratch(t);
  assert.throws(
    () => resolveBackendCommand({ repoRoot: root, stateDir: root, environment: { ORPHEUS_HARNESS_BACKEND_EXE: path.join(root, "missing.exe") } }),
    /does not exist/,
  );
});

test("defaults to the checkout backend module", (t) => {
  const root = scratch(t);
  const python = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  fs.mkdirSync(path.dirname(python), { recursive: true });
  fs.writeFileSync(python, "");
  const resolved = resolveBackendCommand({ repoRoot: root, stateDir: path.join(root, "state"), environment: {} });
  assert.equal(resolved.command, python);
  assert.equal(resolved.cwd, root);
  assert.deepEqual(resolved.args.slice(0, 2), ["-m", "apps.launchers.desktop_backend"]);
});

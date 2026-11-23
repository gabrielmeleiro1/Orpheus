// Run a Python script with the repository's interpreter.
//
// Build and install steps cannot rely on a bare `python` on PATH: current
// macOS ships only `python3`, and an unrelated interpreter would build the
// sidecar against the wrong packages. Prefer the checkout's .venv, then fall
// back to whatever interpreter is active on PATH (CI sets one up without a
// .venv).
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");

function pythonCandidates(platform = process.platform) {
  const isWindows = platform === "win32";
  const venvPython = isWindows
    ? join(repoRoot, ".venv", "Scripts", "python.exe")
    : join(repoRoot, ".venv", "bin", "python");
  const candidates = existsSync(venvPython) ? [venvPython] : [];
  // On Windows `python3` is often the Microsoft Store stub, so try `python` first.
  candidates.push(...(isWindows ? ["python", "python3"] : ["python3", "python"]));
  return candidates;
}

function run(candidates, args) {
  const [command, ...rest] = candidates;
  const child = spawn(command, args, { stdio: "inherit" });
  const forward = (signal) => {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill(signal);
    }
  };
  const signals = ["SIGINT", "SIGTERM", "SIGHUP"];
  for (const signal of signals) {
    process.on(signal, forward);
  }
  const detach = () => {
    for (const signal of signals) {
      process.off(signal, forward);
    }
  };
  let failed = false;
  child.on("error", (error) => {
    failed = true;
    detach();
    if (error.code === "ENOENT" && rest.length > 0) {
      run(rest, args);
      return;
    }
    console.error(`run-python: could not start ${command}: ${error.message}`);
    process.exitCode = 127;
  });
  child.on("exit", (code, signal) => {
    if (failed) {
      return;
    }
    detach();
    if (signal) {
      process.kill(process.pid, signal);
      return;
    }
    process.exitCode = code ?? 1;
  });
}

run(pythonCandidates(), process.argv.slice(2));

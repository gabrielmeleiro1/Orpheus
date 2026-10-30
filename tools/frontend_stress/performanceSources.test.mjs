import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import test from "node:test";

const desktopRequire = createRequire(new URL("../../desktop/package.json", import.meta.url));
const { build } = desktopRequire("esbuild");
const sourceModule = process.env.ORPHEUS_PERF_TEST_SOURCE
  ? pathToFileURL(path.join(process.env.ORPHEUS_PERF_TEST_SOURCE, "tools/frontend_stress/lib/performanceSources.mjs"))
  : new URL("./lib/performanceSources.mjs", import.meta.url);
const {
  performanceSourceOptions,
  performanceSourceProvenance,
  validatePerformanceDependencies,
} = await import(sourceModule.href);

test("selected app source uses the same workload and main dependencies without baseline node_modules", async (t) => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "orpheus-perf-sources-"));
  t.after(() => fs.rm(temporary, { recursive: true, force: true }));
  const repo = path.join(temporary, "main");
  const baseline = path.join(temporary, "baseline");
  const fixturePath = path.join(repo, "desktop/src/test/performanceHarness.tsx");
  const dependency = path.join(repo, "desktop/node_modules/perf-fixture-dependency");
  await fs.mkdir(path.dirname(fixturePath), { recursive: true });
  await fs.mkdir(path.join(baseline, "desktop/src/test"), { recursive: true });
  await fs.mkdir(dependency, { recursive: true });
  await fs.writeFile(path.join(dependency, "package.json"), JSON.stringify({ main: "index.js" }));
  await fs.writeFile(path.join(dependency, "index.js"), "exports.value = 'same-dependency';");
  await fs.writeFile(fixturePath, "import { value } from '../only-in-baseline'; globalThis.result = 'current-workload:' + value;");
  await fs.writeFile(path.join(baseline, "desktop/src/test/performanceHarness.tsx"), "throw new Error('old workload must not run');");
  await fs.writeFile(path.join(baseline, "desktop/src/only-in-baseline.ts"), "import { value as dependency } from 'perf-fixture-dependency'; export const value = 'baseline:' + dependency;");

  const options = await performanceSourceOptions({ repo, sourceRepo: baseline, fixturePath });
  const result = await build({ ...options, absWorkingDir: repo, metafile: true,
    bundle: true, write: false, format: "iife", logLevel: "silent" });
  const dependencies = await validatePerformanceDependencies({ repo, metafile: result.metafile, absWorkingDir: repo });
  assert.equal(dependencies.inputCount, 1);
  assert.equal(dependencies.root, await fs.realpath(path.join(repo, "desktop/node_modules")));
  const { runInNewContext } = await import("node:vm");
  const context = {};
  runInNewContext(result.outputFiles[0].text, context);
  assert.equal(context.result, "current-workload:baseline:same-dependency");
  await assert.rejects(fs.stat(path.join(baseline, "desktop/node_modules")), { code: "ENOENT" });
});

test("missing source fails before bundling instead of silently using current files", async (t) => {
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), "orpheus-perf-missing-"));
  t.after(() => fs.rm(temporary, { recursive: true, force: true }));
  await assert.rejects(performanceSourceOptions({
    repo: temporary, sourceRepo: path.join(temporary, "missing"), fixturePath: path.join(temporary, "fixture.tsx"),
  }), { code: "ENOENT" });
});

async function temporaryDirectory(t, prefix) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), prefix));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  return fs.realpath(directory);
}

test("revision provenance uses the requested commit while retaining separate dirty harness identity", async (t) => {
  const sourceRepo = await temporaryDirectory(t, "orpheus-perf-revision-");
  const harnessHead = "a".repeat(40);
  const selectedCommit = "b".repeat(40);
  const calls = [];
  const git = (args, cwd) => {
    assert.equal(cwd, sourceRepo);
    calls.push(args);
    if (args.join(" ") === "rev-parse --show-toplevel") return sourceRepo;
    if (args.join(" ") === "rev-parse HEAD") return harnessHead;
    if (args.join(" ") === "status --porcelain") return " M desktop/src/changed.ts\n";
    if (args.join(" ") === "rev-parse --verify baseline^{commit}") return selectedCommit;
    throw new Error(`Unexpected Git command: ${args.join(" ")}`);
  };

  const harness = await performanceSourceProvenance({ sourceRepo, git });
  const selected = await performanceSourceProvenance({ sourceRepo, revision: "baseline", git });
  assert.equal(harness.selectedSourceCommit, harnessHead);
  assert.equal(harness.selectedSourceDirty, true);
  assert.equal(harness.selectedSourceMode, "worktree");
  assert.equal(selected.sourceRevision, selectedCommit);
  assert.equal(selected.selectedSourceCommit, selectedCommit);
  assert.equal(selected.selectedSourceDirty, false);
  assert.equal(selected.selectedSourceMode, "revision");
  assert.equal(calls.filter(args => args[0] === "status").length, 1);
});

test("detached worktree provenance records its own clean commit", async (t) => {
  const sourceRepo = await temporaryDirectory(t, "orpheus-perf-detached-");
  await fs.writeFile(path.join(sourceRepo, ".git"), "gitdir: fixture-main/.git/worktrees/baseline\n");
  const commit = "c".repeat(40);
  const git = (args) => {
    if (args.join(" ") === "rev-parse --show-toplevel") return sourceRepo;
    if (args.join(" ") === "rev-parse HEAD") return commit;
    if (args.join(" ") === "status --porcelain") return "";
    throw new Error(`Unexpected Git command: ${args.join(" ")}`);
  };
  const selected = await performanceSourceProvenance({ sourceRepo, git });
  assert.equal(selected.sourceRevision, null);
  assert.equal(selected.selectedSourceMode, "worktree");
  assert.equal(selected.selectedSourceCommit, commit);
  assert.equal(selected.selectedSourceDirty, false);
});

test("source export inside another repository never inherits the parent commit", async (t) => {
  const parent = await temporaryDirectory(t, "orpheus-perf-parent-");
  const sourceRepo = path.join(parent, "export");
  await fs.mkdir(sourceRepo);
  const calls = [];
  const git = (args) => {
    calls.push(args);
    assert.deepEqual(args, ["rev-parse", "--show-toplevel"]);
    return parent;
  };
  const selected = await performanceSourceProvenance({ sourceRepo, git });
  assert.equal(selected.selectedSourceMode, "export");
  assert.equal(selected.selectedSourceCommit, null);
  assert.equal(selected.selectedSourceDirty, null);
  assert.equal(selected.sourceRepo, await fs.realpath(sourceRepo));
  assert.equal(calls.length, 1);
});

test("standalone export records unknown Git identity without failing", async (t) => {
  const sourceRepo = await temporaryDirectory(t, "orpheus-perf-export-");
  const git = () => { throw new Error("fatal: not a git repository"); };
  const selected = await performanceSourceProvenance({ sourceRepo, git });
  assert.equal(selected.selectedSourceMode, "export");
  assert.equal(selected.selectedSourceCommit, null);
  assert.equal(selected.selectedSourceDirty, null);
});

test("unreadable Git metadata for a real worktree is not relabelled as an export", async (t) => {
  const sourceRepo = await temporaryDirectory(t, "orpheus-perf-git-error-");
  await fs.mkdir(path.join(sourceRepo, ".git"));
  const failure = new Error("Git metadata unavailable");
  await assert.rejects(performanceSourceProvenance({ sourceRepo, git: () => { throw failure; } }),
    error => error === failure);
});

test("invalid requested revision fails instead of falling back to HEAD", async (t) => {
  const sourceRepo = await temporaryDirectory(t, "orpheus-perf-bad-revision-");
  const failure = new Error("unknown revision");
  const git = (args) => {
    assert.deepEqual(args, ["rev-parse", "--verify", "missing^{commit}"]);
    throw failure;
  };
  await assert.rejects(performanceSourceProvenance({ sourceRepo, revision: "missing", git }),
    error => error === failure);
});

test("selected source with conflicting installed dependency is rejected before workload execution", async (t) => {
  const temporary = await temporaryDirectory(t, "orpheus-perf-collision-");
  const repo = path.join(temporary, "main");
  const sourceRepo = path.join(temporary, "baseline");
  const fixturePath = path.join(repo, "desktop/src/test/performanceHarness.tsx");
  await fs.mkdir(path.dirname(fixturePath), { recursive: true });
  await fs.mkdir(path.join(sourceRepo, "desktop/src/test"), { recursive: true });
  for (const [directory, value] of [[repo, "current"], [sourceRepo, "conflicting"]]) {
    const dependency = path.join(directory, "desktop/node_modules/perf-fixture-dependency");
    await fs.mkdir(dependency, { recursive: true });
    await fs.writeFile(path.join(dependency, "package.json"), JSON.stringify({ main: "index.js" }));
    await fs.writeFile(path.join(dependency, "index.js"), `exports.value = ${JSON.stringify(value)};`);
  }
  await fs.writeFile(fixturePath, "import { value } from '../app'; globalThis.result = value;");
  await fs.writeFile(path.join(sourceRepo, "desktop/src/app.ts"), "export { value } from 'perf-fixture-dependency';");
  const options = await performanceSourceOptions({ repo, sourceRepo, fixturePath });
  const result = await build({ ...options, absWorkingDir: repo, metafile: true,
    bundle: true, write: false, format: "iife", logLevel: "silent" });
  assert.match(result.outputFiles[0].text, /conflicting/);
  await assert.rejects(validatePerformanceDependencies({ repo, metafile: result.metafile, absWorkingDir: repo }),
    /outside the shared desktop dependency root/);
});

test("default main source bundle passes shared dependency validation", async (t) => {
  const repo = await temporaryDirectory(t, "orpheus-perf-main-deps-");
  const fixturePath = path.join(repo, "desktop/src/test/performanceHarness.tsx");
  const dependency = path.join(repo, "desktop/node_modules/perf-fixture-dependency");
  await fs.mkdir(path.dirname(fixturePath), { recursive: true });
  await fs.mkdir(dependency, { recursive: true });
  await fs.writeFile(path.join(dependency, "package.json"), JSON.stringify({ main: "index.js" }));
  await fs.writeFile(path.join(dependency, "index.js"), "exports.value = 'current';");
  await fs.writeFile(fixturePath, "import { value } from 'perf-fixture-dependency'; globalThis.result = value;");
  const result = await build({ entryPoints: [fixturePath], absWorkingDir: repo, metafile: true,
    bundle: true, write: false, format: "iife", logLevel: "silent" });
  const evidence = await validatePerformanceDependencies({ repo, metafile: result.metafile, absWorkingDir: repo });
  assert.equal(evidence.inputCount, 1);
  assert.equal(evidence.inputs[0], await fs.realpath(path.join(dependency, "index.js")));
});

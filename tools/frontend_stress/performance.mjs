// Production React + headless Chromium benchmark, using synthetic library data.
// It measures rendering/layout work, not model inference or native WebView speed.
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import http from "node:http";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { resolveFallbackChromiumExecutable } from "./lib/chromiumResolver.mjs";
import { performanceSourceOptions, performanceSourceProvenance, validatePerformanceDependencies } from "./lib/performanceSources.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const localRequire = createRequire(import.meta.url);
const desktopRequire = createRequire(path.join(repo, "desktop/package.json"));
const { chromium } = localRequire("playwright");
const { build } = desktopRequire("esbuild");
const args = process.argv.slice(2);
for (const argument of args) {
  if (!/^(--label|--output-dir|--revision|--repo)=.+/.test(argument)) throw new Error(`Unknown argument: ${argument}`);
}
const option = (name, fallback) => args.find((value) => value.startsWith(`${name}=`))?.slice(name.length + 1) ?? fallback;
const label = option("--label", "measurement");
const revision = option("--revision", null);
const selectedRepo = option("--repo", null);
if (revision && selectedRepo) throw new Error("Choose either --revision or --repo");
const harnessProvenance = await performanceSourceProvenance({ sourceRepo: repo });
const selectedProvenance = selectedRepo || revision
  ? await performanceSourceProvenance({ sourceRepo: selectedRepo ?? repo, revision })
  : harnessProvenance;
const { sourceRepo, sourceRevision } = selectedProvenance;
const provenanceCapturedAt = new Date().toISOString();
const output = path.resolve(option("--output-dir", path.join(repo, ".artifacts/local-performance/frontend", `${Date.now()}-${label}`)));
await fs.mkdir(output, { recursive: true });
// Refuse overwriting a previous measurement, including its exact production bundle.
await fs.writeFile(path.join(output, "run.json"), JSON.stringify({ label, started: new Date().toISOString() }), { flag: "wx" });
const fixturePath = path.join(repo, "desktop/src/test/performanceHarness.tsx");
const sourceOptions = selectedRepo
  ? await performanceSourceOptions({ repo, sourceRepo, fixturePath })
  : { entryPoints: [fixturePath] };
const buildResult = await build({
  ...sourceOptions,
  absWorkingDir: repo, metafile: true,
  bundle: true, minify: true, sourcemap: true, format: "iife", target: "es2020",
  outfile: path.join(output, "harness.js"), define: { "process.env.NODE_ENV": '"production"' },
  jsx: "automatic",
  // Bundle historical app files with the current identical workload. Git show
  // is read-only: no checkout, stash, worktree or dependency install is needed.
  plugins: sourceRevision ? [{ name: "revision-sources", setup(build) {
    build.onLoad({ filter: /\.(tsx?|css)$/ }, ({ path: sourcePath }) => {
      if (sourcePath === fixturePath || !sourcePath.startsWith(path.join(repo, "desktop/src") + path.sep)) return;
      const relative = path.relative(repo, sourcePath).split(path.sep).join("/");
      const contents = execFileSync("git", ["show", `${sourceRevision}:${relative}`], { cwd: repo, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 });
      return { contents, loader: path.extname(sourcePath).slice(1) };
    });
  } }] : [],
});
const dependencies = await validatePerformanceDependencies({ repo, metafile: buildResult.metafile, absWorkingDir: repo });
await fs.writeFile(path.join(output, "metafile.json"), JSON.stringify(buildResult.metafile, null, 2));
const assets = new Map([
  ["/", ["text/html", Buffer.from('<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/harness.css"><div id="root"></div><script src="/harness.js"></script>')]],
  ["/harness.js", ["text/javascript", await fs.readFile(path.join(output, "harness.js"))]],
  ["/harness.css", ["text/css", await fs.readFile(path.join(output, "harness.css"))]],
]);
const server = http.createServer((request, response) => {
  const asset = assets.get(request.url);
  if (!asset) { response.writeHead(404); response.end(); return; }
  response.writeHead(200, { "Content-Type": asset[0] }); response.end(asset[1]);
});
await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
let browser;
try {
  const executablePath = resolveFallbackChromiumExecutable({ headless: true });
  browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.waitForFunction(() => Boolean(window.orpheusPerformance));
  const cases = [];
  const playerCases = [];
  for (const chapterCount of [100, 500, 1000]) {
    // The same bundle, fixture and warm-up policy are used before and after.
    await page.evaluate((count) => window.orpheusPerformance.measureLibrary(count, 5000), chapterCount);
    for (let trial = 0; trial < 5; trial += 1) {
      cases.push(await page.evaluate((count) => window.orpheusPerformance.measureLibrary(count, 5000), chapterCount));
    }
    playerCases.push(await page.evaluate((count) => window.orpheusPerformance.measurePlayer(count), chapterCount));
  }
  // Five pages of the shelf, as after "Load more", while a book plays.
  const shelfBookCount = 500;
  await page.evaluate((count) => window.orpheusPerformance.measureShelf(count), shelfBookCount);
  const shelfCases = [];
  for (let trial = 0; trial < 5; trial += 1) {
    shelfCases.push(await page.evaluate((count) => window.orpheusPerformance.measureShelf(count), shelfBookCount));
  }
  if (errors.length) throw new Error(errors.join("\n"));
  const quantile = (values, fraction) => [...values].sort((a, b) => a - b)[Math.floor((values.length - 1) * fraction)];
  const summary = [100, 500, 1000].map((chapterCount) => {
    const rows = cases.filter((row) => row.chapterCount === chapterCount);
    const chapters = rows.flatMap((row) => row.chapterUpdates);
    const edits = rows.flatMap((row) => row.titleEdits);
    return { chapterCount, bookCount: 5000, mountMedianMs: quantile(rows.map((row) => row.mountMs), 0.5),
      chapterUpdateMedianMs: quantile(chapters, 0.5), chapterUpdateP95Ms: quantile(chapters, 0.95),
      titleEditMedianMs: quantile(edits, 0.5), titleEditP95Ms: quantile(edits, 0.95),
      player500TicksMedianMs: quantile(playerCases.find((row) => row.chapterCount === chapterCount).tickBatchMs, 0.5) };
  });
  const shelfTicks = shelfCases.flatMap((row) => row.tickMs);
  const shelfSummary = { bookCount: shelfBookCount, mountMedianMs: quantile(shelfCases.map((row) => row.mountMs), 0.5),
    playbackTickMedianMs: quantile(shelfTicks, 0.5), playbackTickP95Ms: quantile(shelfTicks, 0.95) };
  const report = { label, measuredAt: new Date().toISOString(), ...selectedProvenance, provenanceCapturedAt,
    bundleSha256: createHash("sha256").update(await fs.readFile(path.join(output, "harness.js"))).digest("hex"),
    bundleCssSha256: createHash("sha256").update(await fs.readFile(path.join(output, "harness.css"))).digest("hex"),
    fixtureSha256: createHash("sha256").update(await fs.readFile(fixturePath)).digest("hex"),
    commit: harnessProvenance.selectedSourceCommit, dirty: harnessProvenance.selectedSourceDirty,
    harnessRepo: harnessProvenance.sourceRepo, harnessSourceMode: harnessProvenance.selectedSourceMode,
    dependencies,
    sourceSelectionScope: sourceRevision
      ? "Requested revision for TS/TSX/CSS app modules; current workload and shared installed dependencies."
      : "Selected app tree; current workload and shared installed dependencies.",
    environment: { platform: process.platform, arch: process.arch, cpus: os.cpus()[0]?.model, node: process.version, chromium: browser.version() },
    scope: "Production React render plus synchronous layout, synthetic data; excludes network, inference and native WebView", summary, shelfSummary, cases, playerCases, shelfCases };
  await fs.writeFile(path.join(output, "report.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ output, summary, shelfSummary }, null, 2));
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}

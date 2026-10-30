// Real setup, model switching, conversion and deletion against isolated app data
// and an isolated HF cache. Run from the repo root after npm --prefix desktop run build.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { spawn, execFileSync } from "node:child_process";
import { createInterface } from "node:readline";
import { chromium } from "playwright";
import { SCENARIOS, verifyCudaCleanerChoice } from "./scenarios.mjs";
import { currentTermSelectLabel } from "./lib/termSelect.mjs";
import { BackendProcessError, observeBackendProcess } from "./lib/backendLifecycle.mjs";
import { cleanupAll, registerCleanup } from "./lib/cleanupRegistry.mjs";
import { redactSecrets } from "./lib/util.mjs";

const root = process.cwd();
const artifact = path.join(root, ".artifacts", "model-setup-check", String(Date.now()));
const cache = path.join(artifact, "hf", "hub");
fs.mkdirSync(cache, { recursive: true });
const sourceCache = process.env.HF_HUB_CACHE || path.join(process.env.HF_HOME || path.join(os.homedir(), ".cache", "huggingface"), "hub");
// Hard links save disk space; deleting a test link cannot remove the source file.
function seed(source, destination) {
  fs.mkdirSync(destination, { recursive: true });
  for (const item of fs.readdirSync(source, { withFileTypes: true })) {
    const from = path.join(source, item.name), to = path.join(destination, item.name);
    if (item.isDirectory()) seed(from, to);
    else if (!item.name.includes(".incomplete") && !(source.includes("Qwen2.5-3B-Instruct") && item.name === ".orpheus-download-complete.json")) {
      try { fs.linkSync(fs.realpathSync(from), to); } catch { fs.copyFileSync(from, to); }
    }
  }
}
for (const repo of ["models--hexgrad--Kokoro-82M", "models--Qwen--Qwen2.5-1.5B-Instruct", "models--Qwen--Qwen2.5-3B-Instruct"]) {
  seed(path.join(sourceCache, repo), path.join(cache, repo));
}
const python = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const packaged = process.argv[2];
const executable = packaged ? path.resolve(packaged) : python;
const args = [...(packaged ? [] : ["-m", "apps.launchers.desktop_backend"]), "--host", "127.0.0.1", "--port", "0"];
const env = { ...process.env, PYTHONUTF8: "1", PYTHONFAULTHANDLER: "1", SAVE_CLEANING_DEBUG: "1", ORPHEUS_DESKTOP_DEV: "1", HF_HOME: path.dirname(cache), HF_HUB_CACHE: cache, HF_HUB_OFFLINE: "1", TRANSFORMERS_OFFLINE: "1",
  AUDIOBOOK_DATA_DIR: path.join(artifact, "data"), AUDIOBOOK_OUTPUT_DIR: path.join(artifact, "output"), AUDIOBOOK_TEMP_DIR: path.join(artifact, "temp"), AUDIOBOOK_DB_PATH: path.join(artifact, "data", "books.db"), AUDIOBOOK_DESKTOP_READY_FILE: path.join(artifact, "backend-ready.json") };
const proc = spawn(executable, args, { cwd: root, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
proc.stderr.on("data", (chunk) => fs.appendFileSync(path.join(artifact, "backend.log"), chunk));
const backend = observeBackendProcess(proc, { artifactDir: artifact, executable, args });
registerCleanup(async () => {
  backend.beginCleanup();
  if (proc.pid && proc.exitCode === null && proc.signalCode === null) {
    if (process.platform === "win32") { try { execFileSync("taskkill", ["/PID", String(proc.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore", timeout: 5000 }); } catch {} }
    else proc.kill("SIGTERM");
  }
  let timer;
  try {
    // Capture buffered stderr after exit without waiting on inherited pipes forever.
    await Promise.race([backend.closed, new Promise((resolve) => { timer = setTimeout(resolve, 1000); })]);
  } finally {
    clearTimeout(timer);
  }
});
const results = [], errors = [];
let browser, server, ready, page;
let backendFailed = false;
const safe = (value) => redactSecrets(value, [ready?.token]);
function writeReport() {
  fs.writeFileSync(path.join(artifact, "report.json"), safe(JSON.stringify({ executable, backend: backend.snapshot(), results, pageErrors: errors }, null, 2)));
}
function phase(message) { backend.setPhase(message); writeReport(); }
function pass(step) { results.push({ step, status: "pass" }); writeReport(); }
function check(condition, message) { if (!condition) throw new Error(message); }
async function until(callback, message, timeout = 120000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await callback()) return; await new Promise((resolve) => setTimeout(resolve, 500)); }
  throw new Error(`Timed out: ${message}`);
}
writeReport();
console.log(JSON.stringify({ artifact, backendPid: proc.pid ?? null }));
try {
  let startupTimer, startupReader;
  try {
    ready = await backend.race(() => new Promise((resolve, reject) => {
      startupTimer = setTimeout(() => reject(new Error("Backend startup timeout")), 120000);
      startupReader = createInterface({ input: proc.stdout });
      startupReader.on("line", (line) => {
        let data;
        try { data = JSON.parse(line); } catch { return; }
        if (data?.event === "ready") { backend.markReady(data); resolve(data); }
      });
    }));
  } finally {
    clearTimeout(startupTimer);
    startupReader?.close();
    // Keep draining diagnostics after the one-shot handshake closes its reader.
    proc.stdout.resume();
  }
  const base = `http://${ready.host}:${ready.port}`;
  const get = async (route) => {
    const response = await fetch(base + route, { headers: { Authorization: `Bearer ${ready.token}` } });
    check(response.ok, `${route}: ${response.status}`); return response.json();
  };
  const dist = path.join(root, "desktop", "dist");
  const mime = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2", ".svg": "image/svg+xml" };
  server = http.createServer((req, res) => {
    const name = new URL(req.url, "http://localhost").pathname;
    const file = path.resolve(dist, "." + (name === "/" ? "/index.html" : name));
    if (!file.startsWith(dist + path.sep) || !fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.setHeader("Content-Type", mime[path.extname(file)] || "application/octet-stream"); fs.createReadStream(file).pipe(res);
  });
  registerCleanup(() => new Promise((resolve) => {
    if (!server.listening) { resolve(); return; }
    server.close(resolve);
    server.closeAllConnections();
  }));
  phase("starting frontend and browser");
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(5173, "127.0.0.1", resolve); });
  browser = await chromium.launch({ headless: true, args: ["--host-resolver-rules=MAP localhost 127.0.0.1"] });
  registerCleanup(() => browser.close());
  // Guard the whole journey, including imported scenarios and their long UI waits.
  await backend.race(async () => {
    page = await browser.newPage({ viewport: { width: 1200, height: 860 } });
    page.on("pageerror", (error) => errors.push(String(error)));
    page.on("request", (request) => {
      if (request.method() !== "POST" || new URL(request.url()).pathname !== "/api/conversions") return;
      const options = request.postDataJSON()?.runtime_options;
      if (options) phase(`conversion: ${options.cleaner_model ?? options.cleaner_backend ?? "default cleaner"}, ${options.cleaner_cuda_quantization ?? "default precision"}`);
    });
    phase("first-page runtime selection");
    const url = `http://localhost:5173/?backend=${encodeURIComponent(base)}&token=${encodeURIComponent(ready.token)}`;
    await page.goto(url);
    await page.getByRole("heading", { name: "Welcome to Orpheus" }).waitFor({ timeout: 120000 });
    await page.getByRole("button", { name: "Customize", exact: true }).click();
    await page.locator('input[name="setup-cleaner"]').first().waitFor();
    await page.getByRole("button", { name: "Cleaner precision details" }).click();
    check((await page.locator('[role="tooltip"]').first().textContent()).includes("Quantization stores model weights"), "Precision explanation must be one click away inside Customize");
    await page.keyboard.press("Escape");
    const choose = async (name, text) => page.locator(`label:has(input[name="${name}"])`).filter({ hasText: text }).click();
    await choose("setup-cleaner", "3B");
    await page.locator('input[value="8bit"]').check();
    let plan;
    page.on("request", (request) => { if (request.method() === "POST" && new URL(request.url()).pathname === "/api/setup/models/download") plan = request.postDataJSON(); });
    phase("install selected Qwen 2.5 3B runtime, 8bit");
    await page.locator(".setup-primary").click();
    await page.locator("#tab-btn-settings").waitFor({ timeout: 600000 });
    check(JSON.stringify(plan.model_groups) === JSON.stringify(["tts-cpu-cuda", "cleaner-cuda-large"]), "Only the selected pair may be downloaded");
    check(plan.cleaner_cuda_quantization === "8bit", "Download plan must preserve precision");
    const saved = await get("/api/setup/status");
    const invalid = await fetch(base + "/api/setup/adopt", { method: "POST", headers: { Authorization: `Bearer ${ready.token}`, "Content-Type": "application/json" }, body: JSON.stringify({ profile: "cuda", model_groups: ["cleaner-cuda", "cleaner-cuda-large"] }) });
    check(invalid.status === 422 && (await invalid.text()).includes("Select one local cleaner"), "Packaged backend must reject multiple active cleaners");
    check(saved.cleaner_cuda_quantization === "8bit", "Precision must be saved server-side");
    check(plan.model_groups.filter((id) => id.startsWith("cleaner-")).length === 1, "Exactly one cleaner must be installed");
    phase("reload saved runtime and precision");
    await page.reload();
    await page.locator("#tab-btn-settings").click({ timeout: 120000 });
    await until(async () => (await currentTermSelectLabel(page, "cleaner-model-select")).includes("3B"), "saved model restored");
    check(await page.locator('input[name="cleaner-precision"][value="8bit"]').isChecked(), "Saved precision must be restored");
    pass("first-page selection, installation from cached weights, reload and conversion defaults");
    const ctx = { page, backendBaseUrl: base, token: ready.token, frontendUrl: "http://localhost:5173", fixturesDir: path.join(root, "tests/fixtures"), artifactsDir: artifact, shared: { ttsAvailable: null, bookTitles: [] }, log: console.log, getConsoleErrorCount: () => errors.length, registerSecret: () => {} };
    phase("convert with setup-selected Qwen 2.5 3B cleaner, 8bit");
    await verifyCudaCleanerChoice(ctx, "8bit", 10, "Qwen/Qwen2.5-3B-Instruct");
    pass("real conversion with the cleaner selected during setup");
    phase("switch active cleaner to Qwen 2.5 1.5B, fp16");
    await page.locator("#tab-btn-settings").click();
    await page.locator("#managed-models-heading").scrollIntoViewIfNeeded();
    await choose("managed-cleaner", "1.5B");
    await page.locator('input[value="fp16"]').check();
    await page.getByRole("button", { name: "Save runtime settings", exact: true }).click();
    await until(async () => (await get("/api/setup/status")).cleaner_cuda_quantization === "fp16", "precision-only adoption");
    phase("conversion precision matrix");
    await SCENARIOS.find((item) => item.id === "l").run(ctx);
    pass("four real conversions while switching 4-bit, 8-bit, FP16, 4-bit");
    phase("delete isolated Qwen 2.5 3B model and verify retained books");
    const before = await get("/api/library");
    await page.locator("#tab-btn-settings").click();
    await page.locator("#managed-models-heading").scrollIntoViewIfNeeded();
    await page.getByRole("button", { name: "Delete Qwen 2.5 · 3B", exact: true }).click();
    const [deleted] = await Promise.all([
      page.waitForResponse((response) => response.request().method() === "DELETE" && new URL(response.url()).pathname === "/api/setup/models/cleaner-cuda-large"),
      page.getByRole("button", { name: "Delete model", exact: true }).click(),
    ]);
    check(deleted.ok(), `Model deletion returned ${deleted.status()}`);
    await until(async () => !(await get("/api/setup/models")).models.find((model) => model.id === "cleaner-cuda-large").downloaded, "selected test model deleted");
    const after = await get("/api/library");
    check(JSON.stringify(before.items.map((book) => book.id)) === JSON.stringify(after.items.map((book) => book.id)), "Deleting a model must preserve every book");
    check(fs.existsSync(path.join(sourceCache, "models--Qwen--Qwen2.5-1.5B-Instruct")), "Original model cache must remain intact");
    check((await get("/api/setup/models")).models.find((model) => model.id === "cleaner-cuda").downloaded, "Other installed model must remain intact");
    await page.getByRole("button", { name: "Download Qwen 2.5 · 3B", exact: true }).waitFor();
    pass("delete one model, preserve other models and books, offer reinstall");
    phase("playback after model deletion");
    await page.locator("#tab-btn-library").click();
    await SCENARIOS.find((item) => item.id === "j").run(ctx);
    pass("playback after model deletion");
    check(errors.length === 0, errors.join("; "));
  });
} catch (error) {
  const cause = backend.failure ?? error;
  backendFailed = cause instanceof BackendProcessError;
  const diagnostics = {
    pageUrl: page?.url() ?? null,
    bodyText: page ? await page.locator("body").innerText({ timeout: 2000 }).catch(() => null) : null,
  };
  if (ready && !backendFailed) {
    diagnostics.setupStatus = await fetch(`http://127.0.0.1:${ready.port}/api/setup/status`, {
      headers: { Authorization: `Bearer ${ready.token}` }, signal: AbortSignal.timeout(2000),
    }).then((response) => response.json()).catch(() => null);
  }
  fs.writeFileSync(path.join(artifact, "failure-state.json"), safe(JSON.stringify(diagnostics, null, 2)));
  const message = safe(cause?.stack ?? String(cause));
  results.push({ status: "fail", phase: backend.snapshot().phase, error: message }); console.error(message); process.exitCode = 1;
} finally {
  writeReport();
  console.log(safe(JSON.stringify({ artifact, backend: backend.snapshot(), results })));
  await cleanupAll();
  writeReport();
}
// Imported scenario polls can outlive their rejected race. After evidence and
// bounded cleanup, end this CLI instead of leaving their 600-second timers alive.
if (backendFailed) process.exit(1);

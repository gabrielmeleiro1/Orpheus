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
const env = { ...process.env, PYTHONUTF8: "1", ORPHEUS_DESKTOP_DEV: "1", HF_HOME: path.dirname(cache), HF_HUB_CACHE: cache, HF_HUB_OFFLINE: "1", TRANSFORMERS_OFFLINE: "1",
  AUDIOBOOK_DATA_DIR: path.join(artifact, "data"), AUDIOBOOK_OUTPUT_DIR: path.join(artifact, "output"), AUDIOBOOK_TEMP_DIR: path.join(artifact, "temp"), AUDIOBOOK_DB_PATH: path.join(artifact, "data", "books.db"), AUDIOBOOK_DESKTOP_READY_FILE: path.join(artifact, "backend-ready.json") };
const proc = spawn(executable, args, { cwd: root, env, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
proc.stderr.on("data", (chunk) => fs.appendFileSync(path.join(artifact, "backend.log"), chunk));
const results = [], errors = [];
let browser, server, ready, page;
function check(condition, message) { if (!condition) throw new Error(message); }
async function until(callback, message, timeout = 120000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (await callback()) return; await new Promise((resolve) => setTimeout(resolve, 500)); }
  throw new Error(`Timed out: ${message}`);
}
try {
  ready = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Backend startup timeout")), 120000);
    createInterface({ input: proc.stdout }).on("line", (line) => {
      try { const data = JSON.parse(line); if (data.event === "ready") { clearTimeout(timer); resolve(data); } } catch {}
    });
    proc.once("error", reject);
    proc.once("exit", (code) => { clearTimeout(timer); reject(new Error(`Backend exited ${code}`)); });
  });
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
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(5173, "127.0.0.1", resolve); });
  browser = await chromium.launch({ headless: true, args: ["--host-resolver-rules=MAP localhost 127.0.0.1"] });
  page = await browser.newPage({ viewport: { width: 1200, height: 860 } });
  page.on("pageerror", (error) => errors.push(String(error)));
  const url = `http://localhost:5173/?backend=${encodeURIComponent(base)}&token=${encodeURIComponent(ready.token)}`;
  await page.goto(url);
  await page.getByRole("heading", { name: "Welcome to Orpheus" }).waitFor({ timeout: 120000 });
  await page.locator('input[name="setup-cleaner"]').first().waitFor();
  check((await page.locator("body").textContent()).includes("Quantization stores model weights"), "Precision explanation must appear without Customize");
  const choose = async (name, text) => page.locator(`label:has(input[name="${name}"])`).filter({ hasText: text }).click();
  await choose("setup-cleaner", "3B");
  await page.locator('input[value="8bit"]').check();
  await page.setViewportSize({ width: 1200, height: 1800 });
  await page.screenshot({ path: path.join(artifact, "first-page.png"), fullPage: true });
  await page.setViewportSize({ width: 1200, height: 860 });
  let plan;
  page.on("request", (request) => { if (request.method() === "POST" && new URL(request.url()).pathname === "/api/setup/models/download") plan = request.postDataJSON(); });
  await page.locator(".setup-primary").click();
  await page.locator("#tab-btn-settings").waitFor({ timeout: 600000 });
  check(JSON.stringify(plan.model_groups) === JSON.stringify(["tts-cpu-cuda", "cleaner-cuda-large"]), "Only the selected pair may be downloaded");
  check(plan.cleaner_cuda_quantization === "8bit", "Download plan must preserve precision");
  const saved = await get("/api/setup/status");
  const invalid = await fetch(base + "/api/setup/adopt", { method: "POST", headers: { Authorization: `Bearer ${ready.token}`, "Content-Type": "application/json" }, body: JSON.stringify({ profile: "cuda", model_groups: ["cleaner-cuda", "cleaner-cuda-large"] }) });
  check(invalid.status === 422 && (await invalid.text()).includes("Select one local cleaner"), "Packaged backend must reject multiple active cleaners");
  check(saved.cleaner_cuda_quantization === "8bit", "Precision must be saved server-side");
  check(plan.model_groups.filter((id) => id.startsWith("cleaner-")).length === 1, "Exactly one cleaner must be installed");
  await page.reload();
  await page.locator("#tab-btn-settings").click({ timeout: 120000 });
  await until(async () => (await currentTermSelectLabel(page, "cleaner-model-select")).includes("3B"), "saved model restored");
  check((await currentTermSelectLabel(page, "cleaner-cuda-quantization-select")) === "8bit", "Saved precision must reach conversion");
  results.push({ step: "first-page selection, installation from cached weights, reload and conversion defaults", status: "pass" });
  const ctx = { page, backendBaseUrl: base, token: ready.token, frontendUrl: "http://localhost:5173", fixturesDir: path.join(root, "tests/fixtures"), artifactsDir: artifact, shared: { ttsAvailable: null, bookTitles: [] }, log: console.log, getConsoleErrorCount: () => errors.length, registerSecret: () => {} };
  await verifyCudaCleanerChoice(ctx, "8bit", 10, "Qwen/Qwen2.5-3B-Instruct");
  results.push({ step: "real conversion with the cleaner selected during setup", status: "pass" });
  await page.locator("#tab-btn-settings").click();
  await page.locator("#runtime-setup-btn").click();
  await choose("managed-cleaner", "1.5B");
  await page.locator('input[value="fp16"]').check();
  await page.getByRole("button", { name: "Save runtime settings", exact: true }).click();
  await until(async () => (await get("/api/setup/status")).cleaner_cuda_quantization === "fp16", "precision-only adoption");
  await page.screenshot({ path: path.join(artifact, "model-manager.png"), fullPage: true });
  await page.locator("#setup-back-btn").click();
  await SCENARIOS.find((item) => item.id === "l").run(ctx);
  results.push({ step: "four real conversions while switching 4-bit, 8-bit, FP16, 4-bit", status: "pass" });
  const before = await get("/api/library");
  await page.locator("#tab-btn-settings").click();
  await page.locator("#runtime-setup-btn").click();
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
  await page.getByRole("button", { name: "Install Qwen 2.5 · 3B", exact: true }).waitFor();
  results.push({ step: "delete one model, preserve other models and books, offer reinstall", status: "pass" });
  await page.locator("#setup-back-btn").click();
  await SCENARIOS.find((item) => item.id === "j").run(ctx);
  results.push({ step: "playback after model deletion", status: "pass" });
  check(errors.length === 0, errors.join("; "));
} catch (error) {
  const message = String(error).split(ready?.token || "UNUSED_SECRET").join("[redacted]");
  results.push({ status: "fail", error: message }); console.error(message); process.exitCode = 1;
  await page?.screenshot({ path: path.join(artifact, "failure.png"), fullPage: true }).catch(() => {});
} finally {
  fs.writeFileSync(path.join(artifact, "report.json"), JSON.stringify({ executable, results }, null, 2));
  console.log(JSON.stringify({ artifact, results }));
  await browser?.close();
  await new Promise((resolve) => server?.listening ? server.close(resolve) : resolve());
  if (proc.exitCode === null) {
    if (process.platform === "win32") { try { execFileSync("taskkill", ["/PID", String(proc.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" }); } catch {} }
    else proc.kill("SIGTERM");
  }
}

// Capture every Orpheus screen from the real React app and an isolated backend.
// Usage: node tools/frontend_stress/screens.mjs [--out=<dir>] [--sizes=1600x1000,1280x800] [--scale=2]
// Seeds a small library with real conversions, so local voice models must be cached.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

import { startBackend } from "./lib/backendProcess.mjs";
import { startFrontend } from "./lib/frontendProcess.mjs";
import { resolveFallbackChromiumExecutable } from "./lib/chromiumResolver.mjs";
import { cleanupAll, registerCleanup } from "./lib/cleanupRegistry.mjs";
import { assert, sleep, waitFor } from "./lib/util.mjs";

// An idle backend alone does not prove the seed succeeded: failed/cancelled
// jobs are idle too. Keep the exact submitted job and require its outcome.
export async function waitForScreenConversion(api, jobId, {
  timeoutMs = 300_000,
  intervalMs = 500,
  allowedStatuses = ["completed"],
} = {}) {
  assert(typeof jobId === "string" && jobId.trim(), "Conversion start did not return a job id");
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const active = await api("GET", "/api/conversions/active");
    assert(active.job?.id === jobId, `Expected conversion ${jobId}, received ${active.job?.id ?? "no job"}`);
    if (!active.active) {
      assert(allowedStatuses.includes(active.job.status),
        `Conversion ${jobId} did not complete as expected: ${active.job.status ?? "unknown"}${active.job.error ? `: ${active.job.error}` : ""}`);
      return active;
    }
    await sleep(intervalMs);
  }
  throw new Error(`Conversion ${jobId} did not finish within ${timeoutMs}ms`);
}

async function main() {
const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../..");
const arg = (name, fallback) => process.argv.find((value) => value.startsWith(`--${name}=`))?.slice(name.length + 3) ?? fallback;
const out = path.resolve(arg("out", path.join(repoRoot, ".artifacts", "screens")));
const sizes = arg("sizes", "1600x1000,1280x800").split(",").map((size) => size.split("x").map(Number));
assert(sizes.every((size) => size.length === 2 && size.every((value) => Number.isInteger(value) && value > 0)), "--sizes must contain positive WIDTHxHEIGHT pairs");
const scale = Number(arg("scale", "1"));
assert([1, 2, 3].includes(scale), "--scale must be 1, 2 or 3");
const stateDir = path.join(here, ".state", `screens-${Date.now()}`);
fs.mkdirSync(out, { recursive: true });
fs.mkdirSync(stateDir, { recursive: true });
const log = (message) => console.log(`[screens] ${message}`);

try {
const backend = await startBackend({ repoRoot, stateDir, log });
const frontend = await startFrontend({ desktopDir: path.join(repoRoot, "desktop"), port: 5173, log });
const executablePath = resolveFallbackChromiumExecutable({ headless: true }) ?? undefined;
const browser = await chromium.launch({ headless: true, executablePath, args: ["--host-resolver-rules=MAP localhost 127.0.0.1", "--mute-audio"] });
registerCleanup(() => browser.close());

const api = async (method, route, body) => {
  const response = await fetch(`${backend.baseUrl}${route}`, {
    method,
    headers: { Authorization: `Bearer ${backend.token}`, "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(90_000),
  });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(`${method} ${route} -> ${response.status} ${JSON.stringify(result)}`);
    error.code = result.detail?.code;
    throw error;
  }
  return result;
};
const convert = async (title, author, text, mode = "audiobook") => {
  const upload = await api("POST", "/api/uploads/text", { title, text });
  const payload = { source_path: upload.source_handle, conversion_mode: mode, title, author,
    runtime_options: { cleaner_backend: "basic" } };
  const deadline = Date.now() + 30_000;
  while (true) {
    try {
      const started = await api("POST", "/api/conversions", payload);
      assert(typeof started.job_id === "string" && started.job_id.trim(), "Conversion start did not return a job id");
      return started.job_id;
    } catch (error) {
      // Only an explicit cleanup rejection proves a retry cannot duplicate a job.
      if (error.code !== "conversion_cleanup_pending" || Date.now() >= deadline) throw error;
      await sleep(500);
    }
  }
};

const lighthouse = [
  "# The Lighthouse Keeper", "",
  "## Arrival", "", "The boat left Mara on the narrow quay just after dawn. She carried a single trunk and a lamp that had belonged to her grandmother.", "",
  "## The Light", "", "Every evening at dusk she climbed the steps to the lantern room, trimmed the wick and wound the clockwork that turned the lens.", "",
  "## The Storm", "", "In November the storm came. Waves broke over the lower windows, and Mara kept the light burning through the night.",
].join("\n");
const longText = Array.from({ length: 12 }, (_, index) => `## Part ${index + 1}\n\n${"The river ran past the mill all summer long, and the children learned to swim in the shallows below the weir. ".repeat(18)}`).join("\n\n");
const sampleFile = path.join(stateDir, "The Orchard Letters.md");
fs.writeFileSync(sampleFile, [
  "# The Orchard Letters", "",
  ...Array.from({ length: 9 }, (_, index) => `## Letter ${index + 1}\n\nDear Ana, the apples are early this year and the bees have moved into the old wall again.\n`),
].join("\n"));

async function shoot(page, name) {
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(out, `${name}.png`) });
  log(`saved ${name}`);
}
async function tab(page, id) {
  const button = page.locator(`#tab-btn-${id}`);
  if (!await button.isVisible()) await page.getByRole("button", { name: "Show sidebar", exact: true }).click();
  await button.click();
  await page.locator(id === "settings" ? "#tab-config" : `#tab-${id}`).waitFor({ state: "visible" });
  await page.waitForTimeout(500);
}

  let seeded = false;
  for (const [width, height] of sizes) {
    const suffix = `${width}x${height}`;
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: scale });
    await page.goto(`${frontend.baseUrl}/?backend=${encodeURIComponent(backend.baseUrl)}&token=${encodeURIComponent(backend.token)}`, { timeout: 60_000 });
    if (!seeded) {
      await waitFor(async () => {
        if (await page.locator("#tab-btn-convert").count()) return true;
        const proceed = page.getByRole("button", { name: "Continue", exact: true });
        if (await proceed.isVisible() && await proceed.isEnabled()) {
          await shoot(page, `setup-first-run-${suffix}`);
          await proceed.click();
        }
        return false;
      }, { timeoutMs: 120_000, message: "cached voice runtime allows setup to continue" });
      await tab(page, "library");
      await shoot(page, `library-empty-${suffix}`);
      log("seeding the library");
      await waitForScreenConversion(api, await convert("The Lighthouse Keeper", "Mara Quinn", lighthouse));
      await waitForScreenConversion(api, await convert("Field Notes", "A. Moreau", "Field notes from the coast.\n\nThe tide pools were full of anemones and small crabs this morning.", "document_audio"));
      seeded = true;
    } else {
      await page.locator("#tab-btn-convert").waitFor({ state: "attached", timeout: 120_000 });
    }
    await page.waitForTimeout(1500);

    await tab(page, "convert");
    await shoot(page, `convert-empty-${suffix}`);
    await page.locator(".manual-path summary").click();
    await page.locator("#source-path").fill(sampleFile);
    await page.locator("#source-path").press("Enter");
    await page.locator(".queue-row.is-expanded .book-preview").waitFor({ timeout: 30_000 });
    await page.mouse.move(5, 5);
    await shoot(page, `convert-inspected-${suffix}`);

    await page.locator('#tab-convert input[name="conversion-format"][value="document_audio"]').check();
    await page.locator("#source-input-paste").click();
    await page.locator("#paste-title-input").fill("A new document");
    await page.locator("#paste-text-input").fill("## Notes\n\nA short document, ready to add to the queue.");
    await shoot(page, `create-document-paste-${suffix}`);

    await tab(page, "library");
    const firstBook = page.getByRole("button", { name: "The Lighthouse Keeper, Mara Quinn", exact: true });
    await firstBook.waitFor({ timeout: 30_000 });
    await page.mouse.move(5, 5);
    await shoot(page, `library-${suffix}`);
    await firstBook.click();
    await page.locator(".album-title").waitFor();
    await shoot(page, `library-book-${suffix}`);
    await page.locator(".album-play").click();
    await page.getByRole("button", { name: "Pause audiobook", exact: true }).click();
    await page.locator("#player-now-playing-btn").click();
    const nowPlaying = page.getByRole("dialog", { name: "Now playing", exact: true });
    await nowPlaying.waitFor();
    await shoot(page, `now-playing-${suffix}`);
    await nowPlaying.getByRole("button", { name: "Close now playing", exact: true }).click();

    await tab(page, "settings");
    await shoot(page, `settings-${suffix}`);
    await page.locator(".setup-model-manager").scrollIntoViewIfNeeded();
    await shoot(page, `settings-models-${suffix}`);
    await page.getByRole("region", { name: "Storage", exact: true }).scrollIntoViewIfNeeded();
    await shoot(page, `settings-storage-${suffix}`);

    // Stages and the event log now live in the running conversion's drawer.
    const runningJobId = await convert(`Mill Stories ${suffix}`, "Ines Calder", longText);
    await tab(page, "convert");
    const detailsButton = page.locator(".queue-details-btn").first();
    await detailsButton.waitFor({ timeout: 60_000 });
    await shoot(page, `create-running-${suffix}`);
    await detailsButton.click();
    const details = page.getByRole("dialog", { name: "Conversion details", exact: true });
    await details.waitFor();
    await shoot(page, `conversion-details-${suffix}`);
    await details.getByRole("region", { name: "Event log", exact: true }).scrollIntoViewIfNeeded();
    await shoot(page, `conversion-event-log-${suffix}`);
    await details.getByRole("button", { name: "Close conversion details", exact: true }).click();
    await tab(page, "library");
    await shoot(page, `library-running-${suffix}`);
    const active = await api("GET", "/api/conversions/active");
    assert(active.job?.id === runningJobId, "The running screenshot conversion was replaced unexpectedly");
    if (active.active) await api("POST", `/api/conversions/${runningJobId}/cancel`, {});
    await waitForScreenConversion(api, runningJobId, { allowedStatuses: ["completed", "cancelled"] });
    await page.close();
  }
} finally {
  await cleanupAll();
}
log(`screens written to ${out}`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  await main();
}

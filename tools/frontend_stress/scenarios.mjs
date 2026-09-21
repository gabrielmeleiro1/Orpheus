import fs from "node:fs";
import path from "node:path";
import { startBackend } from "./lib/backendProcess.mjs";

import { assert, sleep, Skip, waitFor, requireSuccessfulConversion, hasRestoredPlaybackOffset } from "./lib/util.mjs";
import {
  chooseTermSelectByIndex,
  chooseTermSelectByLabel,
  currentTermSelectLabel,
  isTermSelectDisabled,
  termSelectOptions,
} from "./lib/termSelect.mjs";


function panelIdForTab(tabId) {
  return tabId === "settings" ? "tab-config" : `tab-${tabId}`;
}

async function gotoTab(page, tabId) {
  await page.locator(`#tab-btn-${tabId}`).click();
}

async function waitTabVisible(page, tabId, timeoutMs = 8000) {
  const id = panelIdForTab(tabId);
  await waitFor(async () => !(await page.locator(`#${id}`).isHidden()), {
    timeoutMs,
    message: `#${id} visible`,
  });
}

async function readProgressMeta(page) {
  const classes = await page.locator("#tab-progress .progress-hero").getAttribute("class").catch(() => "");
  return { uiState: classes?.match(/is-(running|completed|failed|cancelled)/)?.[1] ?? "idle" };
}

async function waitForUiState(page, states, { timeoutMs = 600_000, intervalMs = 1000 } = {}) {
  const targets = Array.isArray(states) ? states : [states];
  return waitFor(
    async () => {
      const { uiState } = await readProgressMeta(page);
      return targets.includes(uiState) ? uiState : null;
    },
    { timeoutMs, intervalMs, message: `uiState in [${targets.join(", ")}]` },
  );
}

async function clearQueue(page, tabId) {
  for (let i = 0; i < 25; i++) {
    if (tabId) {
      const id = panelIdForTab(tabId);
      if (await page.locator(`#${id}`).isHidden()) {
        await gotoTab(page, tabId);
        await waitTabVisible(page, tabId).catch(() => {});
      }
    }

    const rows = page.locator(".queue-row");
    const count = await rows.count();
    if (count === 0) {
      return;
    }
    const removeBtn = rows.first().getByRole("button", { name: /remove/i });
    if ((await removeBtn.count()) === 0 || (await removeBtn.isDisabled())) {
      return;
    }
    try {
      await removeBtn.click({ timeout: 5_000 });
    } catch {
      continue;
    }
    await sleep(150);
  }
}

async function pasteSource(page, { title, text }) {
  const rowsBefore = await page.locator(".queue-row").count();
  await page.locator("#paste-title-input").fill(title);
  await page.locator("#paste-text-input").fill(text);
  await page.locator("#add-pasted-text-btn").click();
  await waitFor(async () => (await page.locator(".queue-row").count()) > rowsBefore, {
    timeoutMs: 10_000,
    message: `queue row appears for pasted source "${title}"`,
  });
}

function readFixture(fixturesDir, ...segments) {
  return fs.readFileSync(path.join(fixturesDir, ...segments), "utf8");
}

async function waitForRuntimeProbeSettled(backendBaseUrl, token, { timeoutMs = 60_000 } = {}) {
  return waitFor(
    async () => {
      const response = await fetch(`${backendBaseUrl}/api/system/capabilities`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(5_000),
      });
      if (!response.ok) {
        return false;
      }
      const body = await response.json();
      return body?.runtime_probe?.state === "fresh" ? body : null;
    },
    { timeoutMs, intervalMs: 2000, message: "backend runtime probe reaches state=fresh" },
  );
}


async function scenarioConnect(ctx) {
  const { page, backendBaseUrl, token, frontendUrl, log } = ctx;

  log("waiting for the backend runtime probe before opening the frontend");
  await waitForRuntimeProbeSettled(backendBaseUrl, token);

  const url = `${frontendUrl}/?backend=${encodeURIComponent(backendBaseUrl)}&token=${encodeURIComponent(token)}`;
  ctx.log("navigating to the isolated frontend (credentials omitted)");
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await waitFor(async () => {
    if (await page.locator("#tab-btn-document").count()) return true;
    const proceed = page.getByRole("button", { name: "Continue", exact: true });
    if (await proceed.count() && await proceed.isEnabled()) await proceed.click();
    return false;
  }, { timeoutMs: 45_000, message: "setup resolves and dashboard opens" });

  await waitFor(
    async () => await page.getByRole("status", { name: "Backend connected", exact: true }).isVisible(),
    { timeoutMs: 20_000, message: 'header status reaches "backend connected"' },
  );
  assert((await page.locator(".status-dot.is-connected").count()) > 0, "expected a connected status dot in the header");

  await gotoTab(page, "convert");
  await waitTabVisible(page, "convert");
  await waitFor(async () => !(await isTermSelectDisabled(page, "tts-engine-select")), {
    timeoutMs: 15_000,
    message: "tts engine options loaded from /api/options/models",
  });

  assert((await page.locator(".status-dot.is-connected").count()) > 0, "connection remains healthy after options load");
}

async function scenarioAddBookPaste(ctx) {
  const { page, fixturesDir, shared } = ctx;
  await gotoTab(page, "document");
  await waitTabVisible(page, "document");

  const pasteLabel = "Stress Short Chapter";
  const expectedTitle = pasteLabel;
  const text = readFixture(fixturesDir, "benchmark_suite", "short_chapter.md");
  await pasteSource(page, { title: pasteLabel, text });

  await waitFor(async () => (await page.locator("#paste-source-status").textContent())?.includes("added"), {
    timeoutMs: 15_000,
    message: "paste status confirms source added",
  });

  const row = page.locator(".queue-row", { hasText: expectedTitle });
  await waitFor(
    async () => {
      const statusText = await row.locator(".queue-item-status").textContent();
      return Boolean(statusText && !statusText.includes("Inspecting"));
    },
    { timeoutMs: 15_000, message: `pasted source preflight settled (expected title "${expectedTitle}")` },
  );

  await row.locator(".queue-item-main").click();
  await waitFor(async () => (await page.locator(".preflight-card").count()) > 0, {
    timeoutMs: 5_000,
    message: "preflight card visible for selected item",
  });
  const meta = await page.locator(".preflight-card .preflight-title").textContent();
  assert(meta && /chapter/.test(meta), `expected chapter/word info in preflight meta, got "${meta}"`);

  shared.pastedBookTitle = expectedTitle;
}

async function scenarioAddBookFiles(ctx) {
  const { page, fixturesDir, shared } = ctx;
  await gotoTab(page, "convert");
  await waitTabVisible(page, "convert");

  const rowsBefore = await page.locator(".queue-row").count();
  const files = [
    path.join(fixturesDir, "sample.epub"),
    path.join(fixturesDir, "sample.pdf"),
    path.join(fixturesDir, "benchmark_suite", "policy_headings.md"),
  ];
  await page.locator('input[type="file"]').setInputFiles([
    ...files.map((file) => ({ name: path.basename(file), mimeType: "application/octet-stream", buffer: fs.readFileSync(file) })),
    { name: "Plain document.txt", mimeType: "text/plain", buffer: Buffer.from("A quiet morning by the lake. We returned home before sunset.") },
  ]);

  await waitFor(async () => (await page.locator(".queue-row").count()) >= rowsBefore + 4, {
    timeoutMs: 30_000,
    message: "queue rows appear for uploaded files",
  });

  const newRows = [0, 1, 2, 3].map((offset) => page.locator(".queue-row").nth(rowsBefore + offset));
  const labels = ["sample.epub", "sample.pdf", "policy_headings.md", "Plain document.txt"];
  const capturedTitles = {};

  for (let i = 0; i < newRows.length; i++) {
    const row = newRows[i];
    await waitFor(
      async () => {
        const statusText = await row.locator(".queue-item-status").textContent();
        return Boolean(statusText && !statusText.includes("Inspecting"));
      },
      { timeoutMs: 20_000, message: `preflight settles for uploaded file #${i} (${labels[i]})` },
    );
    capturedTitles[labels[i]] = (await row.locator(".queue-item-title").textContent())?.trim() ?? "";
  }

  assert(capturedTitles["Plain document.txt"] === "Plain document", "TXT fallback title must use its original filename");
  shared.epubQueueTitle = capturedTitles["sample.epub"];
  shared.pdfQueueTitle = capturedTitles["sample.pdf"];
  shared.uploadedMdTitle = capturedTitles["policy_headings.md"];

  assert(shared.epubQueueTitle, "expected the uploaded epub to settle on some (possibly generated) title");
  assert(shared.pdfQueueTitle, "expected the uploaded pdf to settle on some (possibly generated) title");
  assert(
    shared.uploadedMdTitle === "Part I — Structure",
    `expected policy_headings.md to resolve its content-derived title "Part I — Structure", got "${shared.uploadedMdTitle}"`,
  );
}

async function scenarioOptionsSweep(ctx) {
  const { page } = ctx;
  await gotoTab(page, "convert");
  await waitTabVisible(page, "convert");

  const initialEngineLabel = await currentTermSelectLabel(page, "tts-engine-select");
  const engineLabels = await termSelectOptions(page, "tts-engine-select");
  assert(engineLabels.length > 0, "expected at least one tts engine option from the backend");

  for (let i = 0; i < Math.min(2, engineLabels.length); i++) {
    const label = engineLabels[i];
    await chooseTermSelectByIndex(page, "tts-engine-select", i);
    await waitFor(async () => (await currentTermSelectLabel(page, "tts-engine-select")) === label, {
      timeoutMs: 5_000,
      message: `engine select shows "${label}"`,
    });
    await waitFor(
      async () => {
        const voiceLabel = await currentTermSelectLabel(page, "voice-select");
        return Boolean(voiceLabel && voiceLabel !== "-");
      },
      { timeoutMs: 10_000, message: `voice list settles for engine "${label}"` },
    );
  }

  await chooseTermSelectByLabel(page, "tts-engine-select", initialEngineLabel);
  await waitFor(async () => (await currentTermSelectLabel(page, "tts-engine-select")) === initialEngineLabel, {
    timeoutMs: 5_000,
    message: "engine restored to its initial value",
  });
  await waitFor(
    async () => {
      const voiceLabel = await currentTermSelectLabel(page, "voice-select");
      return Boolean(voiceLabel && voiceLabel !== "-");
    },
    { timeoutMs: 10_000, message: "voice re-settles after engine restore" },
  );

  const voiceLabels = await termSelectOptions(page, "voice-select");
  assert(voiceLabels.length > 0, "expected at least one voice option for the default engine");

  for (let i = 0; i < Math.min(3, voiceLabels.length); i++) {
    const label = voiceLabels[i];
    await chooseTermSelectByIndex(page, "voice-select", i);
    await waitFor(async () => (await currentTermSelectLabel(page, "voice-select")) === label, {
      timeoutMs: 5_000,
      message: `voice select shows "${label}"`,
    });
  }

  if (voiceLabels.length > 1) {
    const needle = voiceLabels[0].slice(0, Math.max(1, Math.min(3, voiceLabels[0].length))).toLowerCase();
    await page.locator("#voice-search").fill(needle);
    await sleep(400);
    const filtered = await termSelectOptions(page, "voice-select");
    assert(filtered.length > 0, `voice filter "${needle}" produced zero matches`);
    assert(filtered.length <= voiceLabels.length, "voice filter should never expand the option list");
    await page.locator("#voice-search").fill("");
    await sleep(400);
  }

  const advancedDetails = page.locator("#advanced-audio-settings");
  const isOpen = await advancedDetails.evaluate((el) => el.open);
  if (!isOpen) {
    await page.locator("#advanced-audio-settings summary").click();
  }
  await waitFor(async () => await page.locator("#speed-input").isVisible(), {
    timeoutMs: 5_000,
    message: "advanced settings panel expands to reveal #speed-input",
  });

  await page.locator("#speed-input").fill("1.25");
  await page.locator("#speed-input").press("Tab");
  assert((await page.locator("#speed-input").inputValue()) === "1.25", "speed input did not accept 1.25");
  await page.locator("#speed-input").fill("1.0");
  await page.locator("#speed-input").press("Tab");

  await gotoTab(page, "settings");
  await waitTabVisible(page, "settings");
  const initialCleanerLabel = await currentTermSelectLabel(page, "cleaner-backend-select");
  const cleanerLabels = await termSelectOptions(page, "cleaner-backend-select");
  assert(cleanerLabels.length > 0, "expected at least one cleaner backend option");

  for (let i = 0; i < Math.min(2, cleanerLabels.length); i++) {
    const label = cleanerLabels[i];
    await chooseTermSelectByIndex(page, "cleaner-backend-select", i);
    await waitFor(async () => (await currentTermSelectLabel(page, "cleaner-backend-select")) === label, {
      timeoutMs: 5_000,
      message: `cleaner backend select shows "${label}"`,
    });
    await sleep(200);
  }

  await chooseTermSelectByLabel(page, "cleaner-backend-select", initialCleanerLabel);
  await waitFor(
    async () => (await currentTermSelectLabel(page, "cleaner-backend-select")) === initialCleanerLabel,
    { timeoutMs: 5_000, message: "cleaner backend restored to its initial value" },
  );

  await gotoTab(page, "convert");
}

async function ensureLocalCleanerBackendSelected(page, log) {
  await gotoTab(page, "settings");
  await waitTabVisible(page, "settings");

  const currentLabel = await currentTermSelectLabel(page, "cleaner-backend-select");
  if (/^local\b/i.test(currentLabel)) {
    await gotoTab(page, "document");
    return;
  }

  const labels = await termSelectOptions(page, "cleaner-backend-select");
  const localLabel = labels.find((label) => /^local\b/i.test(label));
  if (!localLabel) {
    log(`no local cleaner backend option available (options: ${labels.join(", ")}); leaving "${currentLabel}" selected`);
    await gotoTab(page, "document");
    return;
  }

  log(`switching cleaner backend from "${currentLabel}" to "${localLabel}" so conversions don't need an API key`);
  await chooseTermSelectByLabel(page, "cleaner-backend-select", localLabel);
  await waitFor(async () => (await currentTermSelectLabel(page, "cleaner-backend-select")) === localLabel, {
    timeoutMs: 5_000,
    message: `cleaner backend switches to "${localLabel}"`,
  });
  await gotoTab(page, "document");
}

async function scenarioConvertSingle(ctx, source = null) {
  const { page, log, shared } = ctx;
  await ensureLocalCleanerBackendSelected(page, log);
  await gotoTab(page, "document");
  await waitTabVisible(page, "document");

  await clearQueue(page, "document");
  const title = source?.title ?? "Stress Short Chapter";
  await pasteSource(page, { title, text: source?.text ?? "# Narration\n\nThe little boat crossed the quiet lake. We walked home before sunset." });
  const row = page.locator(".queue-row", { hasText: title });
  assert((await row.count()) > 0, "expected the isolated single-source queue item");
  await row.locator(".queue-item-main").click();
  await waitFor(async () => !(await page.locator("#start-btn").isDisabled()), {
    timeoutMs: 10_000,
    message: "start button enabled for the pasted source",
  });

  const errorBefore = (await page.locator("#form-error").textContent()) ?? "";
  await page.locator("#start-btn").click();
  await gotoTab(page, "progress");

  const reachedRunning = await waitFor(
    async () => ["running", "completed"].includes((await readProgressMeta(page)).uiState),
    { timeoutMs: 20_000, message: "job reaches running state" },
  )
    .then(() => true)
    .catch(() => false);

  if (!reachedRunning) {
    const errorAfter = (await page.locator("#form-error").textContent()) ?? "";
    shared.ttsAvailable = false;
    throw new Error(
      `conversion never reached running or completed. form error: "${errorAfter || errorBefore}"`,
    );
  }

  log("conversion accepted; waiting for successful completion");
  let terminal;
  try {
    terminal = await waitForUiState(page, ["completed", "failed", "cancelled"], {
      timeoutMs: 10 * 60_000,
      intervalMs: 2000,
    });
  } catch (err) {
    shared.ttsAvailable = false;
    await page
      .locator(".rail-action.stop")
      .click()
      .catch(() => {});
    throw new Error(`conversion never reached a terminal state within 10 minutes: ${err.message}`);
  }

  requireSuccessfulConversion(terminal);

  shared.ttsAvailable = true;
  await gotoTab(page, "library");
  await waitFor(async () => !(await page.locator("#tab-library").isHidden()), {
    timeoutMs: 10_000,
    message: "library tab opens after completion",
  });
  await waitFor(async () => (await page.locator(".book-row", { hasText: title }).count()) > 0, {
    timeoutMs: 10_000,
    message: "converted book visible in library",
  });
  shared.bookTitles.push(title);
}

async function scenarioCudaCleaner(ctx) {
  // Keep the same app/backend session to catch stale selections or model caches.
  for (const [index, quantization] of ["4bit", "8bit", "fp16", "4bit"].entries()) {
    await verifyCudaCleanerChoice(ctx, quantization, index);
  }
}

export async function verifyCudaCleanerChoice(ctx, quantization, index, modelId = "Qwen/Qwen2.5-1.5B-Instruct") {
  const { page, backendBaseUrl, token, log } = ctx;
  await gotoTab(page, "settings");
  await waitTabVisible(page, "settings");
  const cudaLabel = await waitFor(async () => {
    const labels = await termSelectOptions(page, "cleaner-backend-select");
    return labels.find((label) => /cuda/i.test(label));
  }, { timeoutMs: 60_000, message: "CUDA cleaner options load for the requested GPU test" });
  await chooseTermSelectByLabel(page, "cleaner-backend-select", cudaLabel);
  await chooseTermSelectByLabel(page, "cleaner-model-select", modelId);
  await page.locator("#cleaner-cuda-device-input").fill("Cuda");
  assert(await page.locator("#cleaner-cuda-device-input").inputValue() === "Cuda", "device must display Cuda");
  assert(await page.locator("#cleaner-cuda-device-input").getAttribute("spellcheck") === "false", "GPU identifiers must not be spellchecked");
  await chooseTermSelectByLabel(page, "cleaner-cuda-quantization-select", quantization);

  let submitted = null;
  const capture = (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/conversions") {
      submitted = request.postDataJSON()?.runtime_options;
    }
  };
  page.on("request", capture);
  const title = `CUDA Cleaner ${quantization} Run ${index + 1}`;
  const damaged = "Iam tired. Ibegin again. Iwish the wall would stop moving.";
  try {
    await scenarioConvertSingle(ctx, { title, text: `# Narration\n\n${damaged}` });
  } finally {
    page.off("request", capture);
  }
  for (const [key, value] of Object.entries({
    cleaner_backend: "cuda", cleaner_model: modelId,
    cleaner_cuda_device: "cuda", cleaner_cuda_quantization: quantization,
  })) assert(submitted?.[key] === value, `conversion must submit ${key}=${value}`);

  const get = async (route) => {
    const response = await fetch(`${backendBaseUrl}${route}`, {
      headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(10_000),
    });
    assert(response.ok, `${route} returned ${response.status}`);
    return response.json();
  };
  const library = await get(`/api/library?search=${encodeURIComponent(title)}`);
  const book = library.items.find((item) => item.title === title);
  assert(book, "CUDA conversion must be saved in the library");
  const detail = await get(`/api/library/${book.id}`);
  const traces = [];
  for (const chapter of detail.chapters) {
    const preview = await get(`/api/library/${book.id}/chapters/${chapter.number}/cleaning-preview`);
    traces.push(...preview.trace_rows);
  }
  const cleaned = traces.find((trace) => trace.original_text.includes("Ibegin"));
  assert(cleaned?.mode === "llm_success", `expected actual CUDA inference, got ${cleaned?.mode}`);
  assert(cleaned.cleaned_text.includes("I am tired. I begin again. I wish the wall would stop moving."), "CUDA must repair the extraction artifacts without rewriting prose");
  assert(!cleaned.llm_error, "CUDA cleaner must not silently fall back");
  log(`${quantization}: verified UI selection, submitted precision, saved LLM trace, and corrected text`);
}

async function scenarioConvertCancel(ctx) {
  const { page, shared, log } = ctx;
  await gotoTab(page, "convert");
  await waitTabVisible(page, "convert");

  await gotoTab(page, "document");
  await clearQueue(page, "document");
  await pasteSource(page, { title: "Cancel source", text: Array.from({length: 30}, (_, i) => `# Section ${i}\n\nThe river flowed through the valley. Birds sang in the trees. We walked home together.\n\n`).join("") });
  const row = page.locator(".queue-row", { hasText: "Cancel source" });
  await row.locator(".queue-item-main").click();
  await waitFor(async () => !(await page.locator("#start-btn").isDisabled()), {
    timeoutMs: 10_000,
    message: "start button enabled for the multi-chapter source",
  });

  await page.locator("#start-btn").click();
  await gotoTab(page, "progress");
  const reachedRunning = await waitFor(
    async () => (await readProgressMeta(page)).uiState === "running",
    { timeoutMs: 20_000, message: "conversion reaches running state" },
  )
    .then(() => true)
    .catch(() => false);

  if (!reachedRunning) {
    const err = await page.locator("#form-error").textContent();
    throw new Error(`conversion did not start, cannot exercise cancellation: ${err}`);
  }

  log("conversion running; cancelling mid-flight");
  await sleep(1000);
  assert(!(await page.locator(".rail-action.stop").isDisabled()), "expected the stop control to be enabled while running");
  await page.locator(".rail-action.stop").click();

  await waitForUiState(page, ["cancelled"], { timeoutMs: 60_000, intervalMs: 500 });
  await waitFor(async () => (await page.locator(".rail-action.stop").count()) === 0 || await page.locator(".rail-action.stop").isDisabled(), {
    timeoutMs: 10_000,
    message: "stop control disappears or disables after cancel",
  });
  await gotoTab(page, "document");
  await waitFor(async () => !(await page.locator("#start-btn").isDisabled()), {
    timeoutMs: 10_000,
    message: "start control is usable again after cancel (no stuck job)",
  });
}

async function scenarioQueueRun(ctx) {
  const { page, fixturesDir, shared } = ctx;
  if (shared.ttsAvailable !== true) {
    throw new Error("prerequisite convert-single did not pass");
  }

  await gotoTab(page, "document");
  await waitTabVisible(page, "document");
  await clearQueue(page, "document");

  const sources = [
    { file: "footnotes_stub.md", pasteLabel: "Queue Run A", title: "Queue Run A" },
    { file: "policy_cleaner_noise.md", pasteLabel: "Queue Run B", title: "Queue Run B" },
  ];
  for (const source of sources) {
    await pasteSource(page, { title: source.pasteLabel, text: readFixture(fixturesDir, "benchmark_suite", source.file) });
  }

  await waitFor(async () => !(await page.locator("#run-queue-btn").isDisabled()), {
    timeoutMs: 10_000,
    message: "run-queue button enabled",
  });
  await page.locator("#run-queue-btn").click();

  const outcome = await waitFor(
    async () => {
      for (const source of sources) {
        const statusText = await page
          .locator(".queue-row", { hasText: source.title })
          .locator(".queue-item-status")
          .textContent();
        if (statusText?.toLowerCase().startsWith("failed")) return "failed";
        if (!statusText || !statusText.toLowerCase().includes("done")) {
          return false;
        }
      }
      return true;
    },
    { timeoutMs: 8 * 60_000, intervalMs: 3000, message: "all queued items reach done status" },
  );

  assert(outcome === true, "queued conversion failed");
  for (const source of sources) {
    shared.bookTitles.push(source.title);
  }
}

async function scenarioRapidFire(ctx) {
  const { page, fixturesDir, log } = ctx;
  await gotoTab(page, "document");
  await waitTabVisible(page, "document");
  await clearQueue(page, "document");

  await pasteSource(page, {
    title: "Rapid Fire Source",
    text: Array.from({ length: 30 }, (_, i) => `# Section ${i}\n\nThe river flowed through the valley. Birds sang in the trees. We walked home together.\n\n`).join(""),
  });
  await page.locator(".queue-row", { hasText: "Rapid Fire Source" }).locator(".queue-item-main").click();
  await waitFor(async () => !(await page.locator("#start-btn").isDisabled()), {
    timeoutMs: 10_000,
    message: "start button enabled for rapid-fire source",
  });

  const conversionRequests = [];
  const onRequest = (request) => {
    if (request.method() === "POST" && /\/api\/conversions$/.test(new URL(request.url()).pathname)) {
      conversionRequests.push(request.url());
    }
  };
  page.on("request", onRequest);

  await page.evaluate(() => {
    const btn = document.querySelector("#start-btn");
    for (let i = 0; i < 6; i++) {
      btn?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    }
  });

  await gotoTab(page, "progress");
  const reachedRunning = await waitFor(async () => (await readProgressMeta(page)).uiState === "running", {
    timeoutMs: 20_000,
    message: "rapid-fire job reaches running state",
  })
    .then(() => true)
    .catch(() => false);
  page.off("request", onRequest);

  if (!reachedRunning) {
    throw new Error("rapid-fire source did not start a job");
  }

  assert(
    conversionRequests.length === 1,
    `expected exactly 1 POST /api/conversions from rapid start-btn spam, observed ${conversionRequests.length}`,
  );

  for (let i = 0; i < 2; i++) {
    for (const tab of ["dashboard", "library", "logs", "progress", "convert", "document"]) {
      await page.locator(`#tab-btn-${tab}`).click();
    }
  }
  await gotoTab(page, "document");
  assert((await readProgressMeta(page)).uiState === "running", "job should still be running after rapid tab switching");

  if (!(await page.locator(".manual-path").evaluate((el) => el.open))) await page.locator(".manual-path summary").click();
  for (const fixture of ["footnotes_stub.md", "policy_headings.md"]) {
    await page.locator("#source-path").fill(path.join(fixturesDir, "benchmark_suite", fixture));
    await page.locator("#preflight-source-btn").click();
  }
  const rows = page.locator(".queue-row");
  const rowCount = await rows.count();
  for (let i = 0; i < rowCount; i++) {
    await rows.nth(i).locator(".queue-item-main").click();
  }

  const metadata = page.locator(".metadata-overrides");
  if (!(await metadata.evaluate((el) => el.open))) await metadata.locator("summary").click();
  await page.locator("#title-input").fill("Edited mid-preflight");
  await sleep(2000);
  assert((await page.locator("#title-input").inputValue()) === "Edited mid-preflight", "late preflight overwrote the user title");

  log("cleaning up: cancelling the still-running rapid-fire job");
  await page
    .locator(".rail-action.stop")
    .click()
    .catch(() => {});
  await gotoTab(page, "progress");
  await waitForUiState(page, ["cancelled", "completed"], { timeoutMs: 60_000, intervalMs: 500 });
  await clearQueue(page, "document");
}

async function scenarioPlayerSmoke(ctx) {
  const { page, shared } = ctx;
  if (shared.ttsAvailable !== true || shared.bookTitles.length === 0) {
    throw new Error("required converted book is missing");
  }

  const title = shared.bookTitles[shared.bookTitles.length - 1];
  shared.playerBookTitle = title;

  await gotoTab(page, "library");
  await waitTabVisible(page, "library");
  const row = page.locator(".book-row", { hasText: title });
  await waitFor(async () => (await row.count()) > 0, { timeoutMs: 15_000, message: `library row for "${title}"` });
  await row.first().click();
  await waitFor(async () => (await page.locator("#book-title-edit").inputValue())?.includes(title), {
    timeoutMs: 10_000,
    message: "book detail pane loaded",
  });

  await waitFor(async () => !(await page.locator("#play-btn").isDisabled()), {
    timeoutMs: 15_000,
    message: "player controls enabled (playback output ready)",
  });

  await page.locator("#player-volume").focus();
  await page.keyboard.press("Home");
  await waitFor(async () => (await page.locator("#player-volume").inputValue()) === "0", {
    timeoutMs: 5_000,
    message: "volume slider reaches 0 (muted)",
  });

  await page.locator("#play-btn").click();
  await waitFor(async () => await page.locator("#audio-player").evaluate((el) => !el.paused), {
    timeoutMs: 10_000,
    message: "audio element transitions to playing",
  });
  await sleep(2000);
  await page.locator("#play-btn").click();
  await waitFor(async () => await page.locator("#audio-player").evaluate((el) => el.paused), {
    timeoutMs: 5_000,
    message: "audio element transitions to paused",
  });

  const beforeSeek = await page.locator("#audio-player").evaluate((el) => el.currentTime);
  await page.locator("#seek-fwd-btn").click();
  await waitFor(async () => (await page.locator("#audio-player").evaluate((el) => el.currentTime)) >= beforeSeek, {
    timeoutMs: 5_000,
    message: "seek-forward moves the playhead",
  });
  await page.locator("#seek-back-btn").click();

  for (let i = 0; i < 3; i++) {
    if (!(await page.locator("#next-btn").isDisabled())) {
      await page.locator("#next-btn").click();
    }
    if (!(await page.locator("#prev-btn").isDisabled())) {
      await page.locator("#prev-btn").click();
    }
  }
  await sleep(1000);

  await row.first().click();
  await waitFor(async () => (await page.locator("#book-title-edit").inputValue())?.includes(title), {
    timeoutMs: 10_000,
    message: "re-selected book detail loaded",
  });
  const chapterRows = page.locator("#chapter-list .chapter-main");
  const chapterCount = await chapterRows.count();
  if (chapterCount > 1) {
    for (let i = 0; i < 4; i++) {
      await chapterRows.nth(i % chapterCount).click();
    }
    await sleep(1000);
  }

  assert(
    !(await page.locator("#audio-player").evaluate((el) => Boolean(el.error))),
    "audio element reported a media error after rapid navigation",
  );
  if (!(await page.locator("#audio-player").evaluate((el) => el.paused))) await page.locator("#play-btn").click();
  await page.locator("#player-seek").fill("4");
  await page.locator("#player-seek").dispatchEvent("change");
  await waitFor(async () => Math.abs(await page.locator("#audio-player").evaluate((el) => el.currentTime) - 4) < 0.5, { message: "seek reaches four seconds" });
  await sleep(700);
  await page.reload();
  await page.locator("#tab-btn-library").waitFor({ timeout: 35_000 });
  await gotoTab(page, "library");
  await page.locator(".book-row", { hasText: title }).first().click();
  await page.locator("#audio-player").evaluate((el) => {
    el.addEventListener("play", () => { el.dataset.resumeStarted = String(performance.now()); }, { once: true });
  });
  await page.locator("#play-btn").click();
  await waitFor(async () => {
    const snapshot = await page.locator("#audio-player").evaluate((el) => ({
      currentTime: el.currentTime,
      playingFor: Math.max(0, (performance.now() - Number(el.dataset.resumeStarted)) / 1000),
    }));
    return hasRestoredPlaybackOffset(snapshot);
  }, { timeoutMs: 1_500, intervalMs: 50, message: "saved four-second offset is restored, excluding naturally elapsed playback" });
  await page.locator("#play-btn").click();
}

async function scenarioLibraryCrud(ctx) {
  const { page, shared, frontendUrl, backendBaseUrl, token } = ctx;
  if (shared.ttsAvailable !== true || shared.bookTitles.length === 0) {
    throw new Error("required converted book is missing");
  }

  const candidates = shared.bookTitles.filter((candidate) => candidate !== shared.playerBookTitle);
  const title = candidates[0] || shared.bookTitles[0];

  await gotoTab(page, "library");
  await waitTabVisible(page, "library");
  const row = page.locator(".book-row", { hasText: title });
  await waitFor(async () => (await row.count()) > 0, { timeoutMs: 15_000, message: `library row for "${title}"` });
  await row.first().click();
  await waitFor(async () => (await page.locator("#book-title-edit").inputValue()) === title, {
    timeoutMs: 10_000,
    message: "edit form hydrated with the selected book",
  });

  const newTitle = `${title} (edited)`;
  const newAuthor = "Stress Harness Author";
  await page.locator("#book-title-edit").fill(newTitle);
  await page.locator("#book-author-edit").fill(newAuthor);
  await page.locator("#library-save-metadata-btn").click();
  await waitFor(async () => (await page.locator(".book-row", { hasText: newTitle }).count()) > 0, {
    timeoutMs: 10_000,
    message: "library list reflects the saved title",
  });

  const url = `${frontendUrl}/?backend=${encodeURIComponent(backendBaseUrl)}&token=${encodeURIComponent(token)}`;
  await page.goto(url, { waitUntil: "domcontentloaded" });
  await waitFor(async () => {
    if (await page.locator("#tab-btn-document").count()) return true;
    const proceed = page.getByRole("button", { name: "Continue", exact: true });
    if (await proceed.count() && await proceed.isEnabled()) await proceed.click();
    return false;
  }, { timeoutMs: 45_000, message: "setup resolves and dashboard opens" });
  await waitFor(
    async () => await page.getByRole("status", { name: "Backend connected", exact: true }).isVisible(),
    { timeoutMs: 20_000, message: "reconnected after reload" },
  );
  await gotoTab(page, "library");
  await waitTabVisible(page, "library");
  const reloadedRow = page.locator(".book-row", { hasText: newTitle });
  await waitFor(async () => (await reloadedRow.count()) > 0, {
    timeoutMs: 15_000,
    message: "edited title persisted after reload",
  });
  await reloadedRow.first().click();
  await waitFor(async () => (await page.locator("#book-author-edit").inputValue()) === newAuthor, {
    timeoutMs: 10_000,
    message: "edited author persisted after reload",
  });

  const countBefore = await page.locator(".book-row").count();
  await page.locator("#library-delete-btn").click();
  await page.locator("#library-delete-confirm-btn").click();
  await waitFor(async () => (await page.locator(".book-row", { hasText: newTitle }).count()) === 0, {
    timeoutMs: 10_000,
    message: "deleted book removed from the list",
  });
  const countAfter = await page.locator(".book-row").count();
  assert(countAfter === countBefore - 1, `expected library count to drop by 1 after delete (was ${countBefore}, now ${countAfter})`);

  shared.bookTitles = shared.bookTitles.filter((candidate) => candidate !== title);
}

async function scenarioReconnect(ctx) {
  const { page, log, backend } = ctx;
  if (!backend) {
    throw new Skip("reconnect scenario requires self-started backend mode");
  }

  await gotoTab(page, "dashboard");
  await waitTabVisible(page, "dashboard");

  const errorsBefore = ctx.getConsoleErrorCount();
  log("killing the backend process to simulate a crash mid-session");
  await backend.stop();

  await waitFor(async () => await page.getByRole("status", { name: "Backend error", exact: true }).isVisible(), {
    timeoutMs: 30_000, message: "backend failure becomes an explicit error state",
  });
  const delta = ctx.getConsoleErrorCount() - errorsBefore;
  assert(delta < 50, `expected bounded disconnected polling, saw ${delta} console errors`);
  const replacement = await startBackend({ repoRoot: backend.repoRoot, stateDir: backend.stateDir, log });
  ctx.registerSecret(replacement.token);
  ctx.backend = replacement;
  ctx.backendBaseUrl = replacement.baseUrl;
  ctx.token = replacement.token;
  await page.goto(`${ctx.frontendUrl}/?backend=${encodeURIComponent(replacement.baseUrl)}&token=${encodeURIComponent(replacement.token)}`);
  await waitFor(async () => (await page.locator(".status-dot.is-connected").count()) > 0, {
    timeoutMs: 45_000, message: "restarted backend reconnects with its rotated endpoint",
  });
  await gotoTab(page, "library");
  await waitFor(async () => (await page.locator(".book-row").count()) > 0, { timeoutMs: 15_000, message: "library survives backend restart" });

}


export const SCENARIOS = [
  { id: "a", name: "connect", run: scenarioConnect, allowConsoleErrors: false },
  { id: "b", name: "add-book-paste", run: scenarioAddBookPaste, allowConsoleErrors: false },
  { id: "c", name: "add-book-files", run: scenarioAddBookFiles, allowConsoleErrors: false },
  { id: "d", name: "options-sweep", run: scenarioOptionsSweep, allowConsoleErrors: false },
  { id: "e", name: "convert-single", run: scenarioConvertSingle, allowConsoleErrors: false },
  { id: "l", name: "cuda-cleaner", run: scenarioCudaCleaner, allowConsoleErrors: false, optIn: true },
  { id: "f", name: "convert-cancel", run: scenarioConvertCancel, allowConsoleErrors: false },
  { id: "g", name: "queue-run", run: scenarioQueueRun, allowConsoleErrors: false },
  { id: "h", name: "rapid-fire", run: scenarioRapidFire, allowConsoleErrors: false },
  { id: "j", name: "player-smoke", run: scenarioPlayerSmoke, allowConsoleErrors: false },
  { id: "i", name: "library-crud", run: scenarioLibraryCrud, allowConsoleErrors: false },
  { id: "k", name: "reconnect", run: scenarioReconnect, allowConsoleErrors: true },
];

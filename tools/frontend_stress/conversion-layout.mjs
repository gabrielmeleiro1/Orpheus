// Browser regression for source additions moving the app shell or leaving gaps.
// Uses production views/CSS with synthetic source data and no running backend.
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { launchChromium } from "./lib/chromiumResolver.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const desktopRequire = createRequire(path.join(repo, "desktop/package.json"));
const { build } = desktopRequire("esbuild");
const output = path.join(repo, ".artifacts/conversion-layout-regression");
await fs.mkdir(output, { recursive: true });
await build({
  entryPoints: [path.join(repo, "desktop/src/test/conversionLayoutHarness.tsx")],
  bundle: true, jsx: "automatic", format: "iife", minify: true,
  outfile: path.join(output, "harness.js"), define: { "process.env.NODE_ENV": '"production"' },
});
const assets = new Map([
  ["/", ["text/html", '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/harness.css"><div id="root"></div><script src="/harness.js"></script>']],
  ["/harness.js", ["text/javascript", await fs.readFile(path.join(output, "harness.js"))]],
  ["/harness.css", ["text/css", await fs.readFile(path.join(output, "harness.css"))]],
]);
const server = http.createServer((request, response) => {
  const asset = assets.get(new URL(request.url, "http://localhost").pathname);
  response.writeHead(asset ? 200 : 404, { "Content-Type": asset?.[0] ?? "text/plain" });
  response.end(asset?.[1]);
});
await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
let browser;
const results = [];
try {
  browser = await launchChromium(chromium, { headless: true });
  for (const [width, height] of [[1920, 1030], [1440, 900], [1280, 800], [1024, 768], [800, 600], [640, 480], [3840, 2056]]) {
    for (const documentMode of [false, true]) {
      const name = `${documentMode ? "document" : "convert"}-${width}x${height}`;
      const page = await browser.newPage({ viewport: { width, height }, reducedMotion: "reduce" });
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const settled = () => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      const checkShell = async (stage) => {
        await settled();
        const metrics = await page.evaluate(() => {
          const rect = (selector) => {
            const element = document.querySelector(selector);
            if (!element) return null;
            const box = element.getBoundingClientRect();
            return { top: box.top, bottom: box.bottom, left: box.left, right: box.right, width: box.width, height: box.height, scrollTop: element.scrollTop, scrollLeft: element.scrollLeft };
          };
          const main = document.querySelector(".main-workspace");
          const mainStyle = getComputedStyle(main);
          const voice = document.querySelector(".conversion-narration");
          const voiceStyle = getComputedStyle(voice);
          return {
            frame: rect(".app-frame"), header: rect(".studio-status"), actions: rect(".conversion-actions"),
            pageHeader: rect(".page-header"),
            workspace: rect(".workspace-shell"), main: rect(".main-workspace"), panel: rect(".conversion-panel"),
            scroll: rect(".conversion-scroll"), form: rect(".conversion-form"),
            book: rect(".queue-item-detail"), narration: rect(".conversion-narration"),
            source: rect(".conversion-source-sections"), options: rect(".conversion-options-sections"),
            sourceCard: rect(".conversion-source-section"), queue: rect(".queue-panel"),
            sourceHeader: rect(".source-input-switch"),
            voiceHeader: rect(".voice-options .card-header"),
            inspect: rect("#preflight-source-btn"), actionRow: rect(".conversion-action-row"),
            voiceInset: parseFloat(voiceStyle.paddingBottom) + parseFloat(voiceStyle.borderBottomWidth),
            availableWidth: main.clientWidth - parseFloat(mainStyle.paddingLeft) - parseFloat(mainStyle.paddingRight),
            pageMaxWidth: parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--page-max-width")),
            columnGap: parseFloat(getComputedStyle(document.querySelector(".conversion-form")).columnGap),
            expectedGap: parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--space-8")),
            actionsInsideVoice: document.querySelector(".conversion-actions").parentElement === voice && voice.classList.contains("card"),
            inspectInsideRow: document.querySelector("#preflight-source-btn")?.closest(".queue-row.is-expanded") !== null,
            pathInsideSources: document.querySelector(".manual-path").closest(".card") === document.querySelector(".conversion-source-section"),
            railBottom: document.querySelector(".rail").hidden ? null : rect(".rail-bottom"),
            horizontalOverflow: Object.fromEntries([".conversion-form", ".conversion-scroll", ".conversion-source-sections", ".conversion-options-sections", ".queue-panel", ".conversion-narration", ".queue-list"].map((selector) => {
              const element = document.querySelector(selector);
              return [selector, element.scrollWidth - element.clientWidth];
            })),
            rootScroll: [document.documentElement.scrollTop, document.body.scrollTop, window.scrollY, window.scrollX],
            viewport: { width: innerWidth, height: innerHeight },
          };
        });
        const context = `${name}: ${stage}`;
        assert.deepEqual(metrics.rootScroll, [0, 0, 0, 0], `${context}: document moved`);
        assert.equal(metrics.frame.top, 0, `${context}: app top clipped`);
        assert.equal(metrics.frame.bottom, metrics.viewport.height, `${context}: blank space below app`);
        assert.ok(Math.abs(metrics.header.top - metrics.workspace.top) <= 1, `${context}: header moved outside workspace`);
        assert.ok(metrics.workspace.top >= 0 && metrics.workspace.top <= 12, `${context}: workspace moved`);
        assert.equal(metrics.main.scrollTop, 0, `${context}: outer workspace scrolled`);
        assert.equal(metrics.panel.scrollTop, 0, `${context}: outer panel scrolled`);
        assert.ok(Math.abs(metrics.panel.width - Math.min(metrics.availableWidth, metrics.pageMaxWidth)) <= 1, `${context}: page does not use the shared width cap`);
        assert.ok(Math.abs(metrics.panel.left - metrics.pageHeader.left) <= 1 && Math.abs(metrics.panel.right - metrics.pageHeader.right) <= 1, `${context}: page and heading edges differ (page ${metrics.panel.left}-${metrics.panel.right}, heading ${metrics.pageHeader.left}-${metrics.pageHeader.right})`);
        assert.ok(Math.abs((metrics.panel.left + metrics.panel.right) - (metrics.main.left + metrics.main.right)) <= 1, `${context}: page is not centred`);
        if (metrics.railBottom) assert.ok(Math.abs(metrics.viewport.height - metrics.railBottom.bottom - metrics.workspace.top) <= 1, `${context}: sidebar bottom moved`);
        assert.ok(metrics.form.width >= metrics.panel.width - 17, `${context}: unused form width`);
        assert.ok(Object.values(metrics.horizontalOverflow).every((value) => value <= 1), `${context}: horizontal overflow ${JSON.stringify(metrics.horizontalOverflow)}`);
        assert.equal(metrics.columnGap, metrics.expectedGap, `${context}: inconsistent workbench spacing`);
        assert.ok(metrics.actionsInsideVoice, `${context}: actions are outside the Voice card`);
        assert.ok(metrics.pathInsideSources, `${context}: path disclosure is outside the Sources card`);
        assert.ok(metrics.options.bottom <= metrics.actions.top, `${context}: actions cover narration settings`);
        assert.ok(Math.abs(metrics.actions.bottom - (metrics.narration.bottom - metrics.voiceInset)) <= 1, `${context}: actions not pinned inside the Voice card`);
        assert.ok(Math.abs(metrics.actions.width - metrics.options.width) <= 1, `${context}: Voice controls and actions have different widths`);
        if (metrics.inspect) {
          // The book's details expand inside its own queue row; Inspect sits at that row's right edge.
          assert.ok(metrics.inspectInsideRow, `${context}: Inspect is outside the selected queue row`);
          assert.ok(Math.abs(metrics.inspect.right - metrics.book.right) <= 1, `${context}: Inspect is not right-aligned in the book details`);
        }
        if (metrics.panel.width >= 720) {
          assert.ok(metrics.actions.bottom <= metrics.viewport.height, `${context}: actions below viewport`);
          assert.ok(metrics.actions.top >= metrics.panel.top, `${context}: actions above workspace`);
          assert.equal(metrics.scroll.top, metrics.narration.top, `${context}: narration and book workspace not aligned`);
          assert.ok(Math.abs(metrics.scroll.height - metrics.narration.height) <= 1, `${context}: workbench columns have different heights`);
          assert.ok(metrics.narration.left > metrics.scroll.right, `${context}: narration overlaps book workspace`);
          if (metrics.book?.height > 0) {
            assert.ok(metrics.book.top >= metrics.queue.top, `${context}: selected book details must be inside the queue`);
          }
        } else {
          assert.ok(metrics.options.top >= metrics.source.bottom, `${context}: narrow layout columns overlap`);
          assert.ok(Math.abs(metrics.narration.width - metrics.scroll.width) <= 1, `${context}: narrow cards have different widths`);
        }
        // One action row: the primary, or Cancel while a conversion runs.
        const primary = await page.locator("#start-btn:visible, #run-queue-btn:visible, #cancel-btn:visible").first().boundingBox();
        assert.ok(primary && Math.abs(metrics.actionRow.width - metrics.actions.width) <= 1, `${context}: action row is not full width`);
        assert.ok(Math.abs(primary.x + primary.width - metrics.actionRow.right) <= 1, `${context}: primary action is not at the row's right edge`);
        assert.ok(primary.y >= metrics.actions.top && primary.y + primary.height <= metrics.actions.bottom, `${context}: primary action is clipped by footer`);
        for (const button of await page.locator(".conversion-actions button:visible").all()) {
          const box = await button.boundingBox();
          assert.ok(box.y >= metrics.actions.top && box.y + box.height <= metrics.actions.bottom, `${context}: action button clipped by footer`);
        }
        return metrics;
      };
      try {
        await page.goto(`http://127.0.0.1:${server.address().port}/${documentMode ? "?document" : ""}`);
        await page.waitForFunction(() => Boolean(window.conversionLayout));
        await checkShell("empty");
        await page.getByRole("button", { name: "Drop file here" }).click();
        await checkShell("source added");
        const inspect = page.getByRole("button", { name: "Inspect", exact: true });
        await inspect.scrollIntoViewIfNeeded();
        const beforeInspection = await page.locator(".conversion-scroll").evaluate((element) => element.scrollTop);
        await inspect.click();
        await page.locator(".book-preview summary").click();
        let metrics = await checkShell("inspection complete");
        assert.equal(await page.locator(".book-chapters li").count(), 12, `${name}: chapter preview missing`);
        await page.getByText("3 more", { exact: true }).waitFor();
        assert.equal(metrics.scroll.scrollTop, beforeInspection, `${name}: source inspection moved the user's viewport`);
        await page.screenshot({ path: path.join(output, `${name}-prepared.png`) });
        await page.evaluate(() => { window.conversionLayout.addSource(); window.conversionLayout.inspect(); window.conversionLayout.rename(); });
        await checkShell("batch and long title");
        const beforeHidden = await page.locator(".conversion-scroll").evaluate((element) => element.scrollTop);
        await page.evaluate(() => window.conversionLayout.hide(true));
        await settled();
        await page.evaluate(() => { window.conversionLayout.inspect(); window.conversionLayout.hide(false); });
        metrics = await checkShell("tab restored");
        assert.equal(metrics.scroll.scrollTop, beforeHidden, `${name}: tab restoration moved the user's viewport`);
        await page.evaluate(() => window.conversionLayout.checking(true));
        await checkShell("checking and cancelling");
        await page.locator(".conversion-queue-summary", { hasText: "Checking…" }).waitFor();
        await page.getByRole("button", { name: "Cancelling…", exact: true }).waitFor();
        await page.evaluate(() => window.conversionLayout.checking(false));
        // Focus and dropdown navigation still reveal controls inside the form.
        await page.locator("#advanced-audio-settings").evaluate((element) => { element.open = true; });
        await page.locator("#parallel-worker-max-input").focus();
        await checkShell("keyboard focus");
        await page.locator("#voice-select button").click();
        await page.keyboard.press("End");
        await checkShell("long voice menu");
        await page.keyboard.press("Escape");
        await page.locator(".conversion-scroll").evaluate((element) => { element.scrollTop = element.scrollHeight; });
        metrics = await checkShell("scrolled to end");
        // The footer remains available while either settings or sources scroll.
        await page.locator(".conversion-options-sections").evaluate((element) => { element.scrollTop = element.scrollHeight; });
        await page.locator(".conversion-form").evaluate((element) => { element.scrollTop = element.scrollHeight; });
        await checkShell("settings and workspace scrolled");
        await page.evaluate(() => window.conversionLayout.fillQueue());
        metrics = await checkShell("long queue");
        assert.equal(await page.locator(".queue-row").count(), 25, `${name}: queue entries lost`);
        assert.ok(await page.evaluate(() => [".conversion-scroll", ".conversion-form"].some((selector) => {
          const element = document.querySelector(selector);
          return element.scrollHeight > element.clientHeight;
        })), `${name}: long queue is not scrollable`);
        await page.locator("#run-queue-btn").scrollIntoViewIfNeeded();
        await checkShell("primary action revealed");
        const revealedAction = await page.locator("#run-queue-btn").boundingBox();
        assert.ok(revealedAction.y >= 0 && revealedAction.y + revealedAction.height <= height, `${name}: primary action cannot be revealed`);
        await page.setViewportSize({ width: 900, height: 650 });
        await checkShell("resized while populated");
        assert.deepEqual(errors, [], `${name}: browser errors`);
        await page.screenshot({ path: path.join(output, `${name}.png`) });
        results.push({ name, passed: true });
      } catch (error) {
        await page.screenshot({ path: path.join(output, `${name}-failure.png`) });
        throw error;
      } finally { await page.close(); }
    }
  }
  await fs.writeFile(path.join(output, "report.json"), JSON.stringify({ browser: browser.version(), results }, null, 2));
  console.log(`Passed ${results.length} conversion layout cases. Artifacts: ${output}`);
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}

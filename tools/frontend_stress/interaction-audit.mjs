// Interaction-state regression for the real Orpheus UI.
//
// Starts an isolated backend and Vite, seeds real states through the backend
// API (mocking through request interception only where a state cannot be
// reached live), then at every audited window size hovers every interactive
// element, walks the page with real Tab presses, and opens every disclosure,
// popup and dialog it finds. After each action it checks geometry
// mechanically: overlays inside the window and unclipped, no unreadable
// truncated text, nothing covering controls, visible focus that returns to its
// trigger, Escape closing overlays, no sideways page scroll and no layout shift.
//
// Usage: node tools/frontend_stress/interaction-audit.mjs [--out=<dir>]
//   [--viewport=960x640,1440] [--state=library-*,settings] [--list-states] [--headed]
// Exits non-zero when any check fails. The JSON report and a screenshot of
// every distinct failure are written to --out.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "playwright";

import { startBackend } from "./lib/backendProcess.mjs";
import { startFrontend } from "./lib/frontendProcess.mjs";
import { launchChromium } from "./lib/chromiumResolver.mjs";
import { cleanupAll, registerCleanup } from "./lib/cleanupRegistry.mjs";
import { assert, sleep, waitFor, waitForConversion, redactSecrets } from "./lib/util.mjs";
import { installInteractionProbe } from "./lib/interactionProbe.mjs";
import { measureFocusIndicator, measureLabelVisibility, measureStateDistinct } from "./lib/interactionPixels.mjs";
import {
  downloadFailed, downloadRunning, failedActive, failedEventStream, libraryCount, libraryPage,
  isGuardedRequest, mockedSuccess, runningActive, runtimeNotReadyStatus,
} from "./lib/interactionMocks.mjs";
import {
  checkClipping, checkCoverage, checkFloatingInViewport, checkFocusIndicator, checkFocusLanding, checkFocusVisible,
  checkForcedLabel, checkForcedState, checkHorizontalScroll, checkSnapshot, checkTooltip, failureKey, hoverTipLingers, layoutShifts,
  parseArgs, rebuiltTwins, selectStates, selectViewports,
} from "./lib/interactionChecks.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "../..");

// Bounded so a 200-book library does not open the same kind of row 200 times;
// every instance is still hovered, focused and measured.
const OPEN_TESTS_PER_KIND = 3;
const TOOLTIP_WAIT_MS = 1_000;
// Rows sharing one control kind and tip text (a chapter's preview button in a
// long album) prove the tooltip a few times; later ones are hovered without
// waiting out the tooltip delay.
const TOOLTIP_WAITS_PER_KIND = 3;
const LONG_ALBUM_CHAPTERS = 120;
const QUICK_VIEWPORTS = "960x640,1920x1080";
const MAX_SCREENSHOTS = 400;
// The app's self-dismissing completion notice (App.tsx COMPLETION_NOTICE_MS).
const COMPLETION_NOTICE_MS = 10_000;
// Holds the self-dismissing notice open for the length of the audit.
const HOLD_COMPLETION_NOTICE = `(() => { const original = window.setTimeout; window.setTimeout = function (handler, delay, ...rest) { return delay === ${COMPLETION_NOTICE_MS} ? original(() => undefined, 2147483647) : original(handler, delay, ...rest); }; })();`;

const LONG_UNBROKEN = "Thequickbrownfoxjumpsoverthelazydog".repeat(6).slice(0, 200);
const LONG_SPACED = "A Remarkably Long Account of the Lighthouse Keepers of the Northern Coast, Their Lamps, Their Letters, and the Winter the Fog Never Lifted From the Harbour at Mara";
const CJK_TITLE = "東京の古い灯台守と海辺の町で過ごした長い冬の日々の記録、そして霧の朝に届いた手紙の物語";
const RTL_AUTHOR = "نجيب محفوظ، راوي الحكايات الطويلة عن البحر والمدينة";
const LONG_AUTHOR = "Professor Emerita Henrietta Wilhelmina Fairweather-Montgomery of the Royal Maritime Institute";
const LONG_ERROR = `Chapter 7 could not be synthesised: the voice runtime reported ${LONG_UNBROKEN} while reading C:\\Users\\reader\\Documents\\Audiobooks\\Very Long Folder Name For Testing\\${LONG_UNBROKEN}.epub`;

const SHORT_TEXT = [
  "# The Lighthouse Keeper", "",
  "## Arrival", "", "The boat left Mara on the narrow quay just after dawn.", "",
  "## The Light", "", "Every evening at dusk she climbed the steps to the lantern room.",
].join("\n");
const LONG_RUNNING_TEXT = Array.from({ length: 60 }, (_, index) => `## Part ${index + 1}\n\n${"The river ran past the mill all summer long, and the children learned to swim in the shallows below the weir. ".repeat(24)}`).join("\n\n");

function log(message) {
  console.log(`[interaction-audit] ${message}`);
}

// ---------------------------------------------------------------------------
// Backend seeding (Node side, never through an audited page).

function createApi(backend) {
  return async function api(method, route, body) {
    const response = await fetch(`${backend.baseUrl}${route}`, {
      method,
      headers: { Authorization: `Bearer ${backend.token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(90_000),
    });
    const text = await response.text();
    const result = text ? JSON.parse(text) : {};
    if (!response.ok) {
      const error = new Error(`${method} ${route} -> ${response.status} ${text.slice(0, 400)}`);
      error.code = result.detail?.code;
      throw error;
    }
    return result;
  };
}

async function startConversion(api, title, author, text) {
  const upload = await api("POST", "/api/uploads/text", { title, text });
  const payload = { source_path: upload.source_handle, conversion_mode: "audiobook", title, author, runtime_options: { cleaner_backend: "basic" } };
  const deadline = Date.now() + 60_000;
  while (true) {
    try {
      const started = await api("POST", "/api/conversions", payload);
      return started.job_id;
    } catch (error) {
      if (error.code !== "conversion_cleanup_pending" || Date.now() >= deadline) throw error;
      await sleep(500);
    }
  }
}

async function libraryItems(api) {
  return (await api("GET", "/api/library")).items;
}

// ---------------------------------------------------------------------------
// Request interception: a destructive-request guard on every audited page,
// plus per-state mocks. Mocked responses are built from the real backend's
// response so CORS and content headers stay authentic.

function backendMatcher(baseUrl) {
  const origin = new URL(baseUrl).origin;
  return (url) => url.origin === origin;
}

// Guarded requests (lib/interactionMocks.mjs) never reach the backend: each is
// answered with a faithful mocked success so the UI completes its transition.
// Outside the activation pass nothing should send one, so there it is also a failure.
async function guardedAnswer(route, request, url) {
  const method = request.method();
  const sent = () => { try { return request.postDataJSON(); } catch { return null; } };
  const fetchGet = async (pathname) => route.fetch({ url: new URL(pathname, url).href, method: "GET", postData: undefined });
  if (method === "PATCH") {
    // A saved edit answers with the book as it now reads.
    const response = await fetchGet(url.pathname);
    return route.fulfill({ response, json: { ...await response.json(), ...sent() } });
  }
  if (method === "POST" && url.pathname === "/api/setup/adopt") {
    const response = await fetchGet("/api/setup/status");
    return route.fulfill({ response, json: { ...await response.json(), configured: true, state: "ready", ready: true } });
  }
  if (method === "POST" && url.pathname.startsWith("/api/conversions/repair/")) {
    const response = await fetchGet("/health");
    return route.fulfill({ response, json: mockedSuccess("POST", "/api/conversions", null) });
  }
  const body = mockedSuccess(method, url.pathname, sent());
  if (!body) return route.abort("blockedbyclient");
  const response = await fetchGet("/health");
  return route.fulfill({ response, json: body });
}

async function installRoutes(context, ctx, state, recordGuard, guard = { activating: false }) {
  const mocks = state.mocks ? state.mocks(ctx) : [];
  await context.route(backendMatcher(ctx.backend.baseUrl), (route) => answer(route).catch((error) => {
    // A request still in flight when its window closes (the next state or a
    // reopened session) has nobody left to answer; anything else is a bug.
    if (/disposed|closed/i.test(String(error?.message))) return;
    throw error;
  }));
  async function answer(route) {
    const request = route.request();
    const url = new URL(request.url());
    for (const mock of mocks) {
      if (mock.method !== "*" && (mock.method ?? "GET") !== request.method()) continue;
      if (!mock.path.test(url.pathname) || (mock.when && !mock.when())) continue;
      if (mock.action === "hang") return; // never answered: the state waits on it
      if (mock.action === "abort") return route.abort("connectionrefused");
      if (mock.action === "stream") return route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream", "Access-Control-Allow-Origin": ctx.frontend.baseUrl }, body: await mock.body(url, route) });
      const response = await route.fetch();
      const body = await mock.json(await response.json().catch(() => null), url);
      return route.fulfill({ response, status: mock.status ?? 200, json: body });
    }
    if (isGuardedRequest(request.method(), url.pathname)
      && !(state.allowRequests ?? []).some((pattern) => pattern.test(`${request.method()} ${url.pathname}`))) {
      if (!guard.activating) recordGuard(`${request.method()} ${url.pathname}`);
      return guardedAnswer(route, request, url);
    }
    return route.continue();
  }
}

// ---------------------------------------------------------------------------
// Page helpers.

const probe = (page, method, ...args) => page.evaluate(([name, values]) => window.__interactionProbe[name](...values), [method, args]);

async function settle(page, ms = 0) {
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  if (ms) await page.waitForTimeout(ms);
}

async function parkMouse(page) {
  // Outside the document: no element keeps a hover style. A hover tip lingers
  // for its grace period (it can be hovered itself); wait it out so the next
  // measurement never reads the previous control's tip.
  await page.mouse.move(-10, -10);
  const deadline = Date.now() + 600;
  for (;;) {
    const tip = await page.evaluate(() => ({
      shown: document.querySelector(".tooltip-bubble")?.textContent ?? null,
      focusedTip: document.activeElement?.closest("[data-tip]")?.getAttribute("data-tip") ?? null,
    }));
    if (!hoverTipLingers(tip) || Date.now() >= deadline) return;
    await page.waitForTimeout(25);
  }
}

async function openApp(page, ctx) {
  await page.goto(`${ctx.frontend.baseUrl}/?backend=${encodeURIComponent(ctx.backend.baseUrl)}&token=${encodeURIComponent(ctx.backend.token)}`, { timeout: 60_000 });
}

async function openDashboard(page, ctx) {
  await openApp(page, ctx);
  await page.locator("#tab-btn-library").waitFor({ state: "attached", timeout: 120_000 });
}

/**
 * The shelf loads more than once just after launch: the dev build mounts its
 * effects twice, and a job restored as finished refreshes the library. Each
 * reload puts the first page back, so paging waits until loading stays quiet.
 */
async function libraryLoadsSettled(page, quietMs = 600) {
  await page.waitForFunction((quiet) => {
    const list = document.querySelector("#library-list");
    if (!list || list.getAttribute("aria-busy") === "true") {
      delete document.documentElement.dataset.libraryQuietSince;
      return false;
    }
    const since = Number(document.documentElement.dataset.libraryQuietSince ??= String(performance.now()));
    return performance.now() - since >= quiet;
  }, quietMs, { polling: "raf", timeout: 60_000 });
  await page.evaluate(() => { delete document.documentElement.dataset.libraryQuietSince; });
}

async function gotoTab(page, id) {
  const button = page.locator(`#tab-btn-${id}`);
  if (!await button.isVisible()) await page.getByRole("button", { name: "Show sidebar", exact: true }).click();
  await button.click();
  await page.locator(id === "settings" ? "#tab-config" : `#tab-${id}`).waitFor({ state: "visible" });
  await settle(page, 300);
}

async function openSettingsSection(page, locator) {
  await gotoTab(page, "settings");
  await locator.waitFor({ timeout: 30_000 });
  await locator.scrollIntoViewIfNeeded();
}

// ---------------------------------------------------------------------------
// States. `live` states run against real backend data; `mocked` ones say how.

function longAlbumMocks() {
  return [{ path: /^\/api\/library\/\d+$/, json: (real) => ({
    ...real,
    total_chapters: LONG_ALBUM_CHAPTERS,
    chapters: Array.from({ length: LONG_ALBUM_CHAPTERS }, (_, index) => ({
      ...real.chapters[index % real.chapters.length],
      id: 10_000 + index,
      number: index + 1,
      title: index % 9 === 4 ? LONG_UNBROKEN : index % 9 === 7 ? CJK_TITLE : `Part ${index + 1}: The river past the mill`,
    })),
  }) }];
}

/** A job that runs, then fails: active answers running once, then failed; the stream replays both. */
function failedJobMocks(ctx) {
  // The app only announces a failure it saw running, and it shows a job as
  // running only once the runtime check has passed. So the event stream holds
  // the failure until the page shows the running job; the active job reads
  // running until then and failed afterwards, like the backend.
  let streamed = false;
  const job = { title: LONG_SPACED, author: LONG_AUTHOR };
  return [
    { path: /^\/api\/conversions\/active$/, json: () => (streamed ? failedActive({ ...job, error: LONG_ERROR }) : runningActive(job)) },
    { path: /^\/api\/events\/stream$/, action: "stream", body: async (_, route) => {
      // Attached, not visible: a compact window keeps the sidebar collapsed.
      await route.request().frame().page().locator(".rail-job").waitFor({ state: "attached", timeout: 60_000 }).catch(() => undefined);
      streamed = true;
      return failedEventStream({ backendInstanceId: ctx.backendInstanceId, error: LONG_ERROR });
    } },
  ];
}

/** 200 books, paged by the request's own limit/offset like the backend. */
function libraryOf200(ctx) {
  let base = null;
  const item = (index) => (index === 0 ? base : libraryItem(base, index, index % 17 === 0 ? { title: LONG_UNBROKEN, author: RTL_AUTHOR } : index % 13 === 0 ? { title: CJK_TITLE } : {}));
  return [
    { path: /^\/api\/library$/, json: (real, url) => {
      base ??= real.items[0];
      return libraryPage(real, url, 200, item);
    } },
    { path: /^\/api\/library\/count$/, json: () => libraryCount(200) },
    { path: /^\/api\/library\/1\d{5}$/, json: (_, url) => ({ ...ctx.realDetail, id: Number(url.pathname.split("/").pop()), title: `Shelf Book ${Number(url.pathname.split("/").pop()) - 99_999}` }) },
  ];
}

function libraryItem(base, index, overrides = {}) {
  return { ...base, id: 100_000 + index, title: `Shelf Book ${index + 1}`, author: index % 5 === 0 ? null : `Author ${index + 1}`, source_path: null, ...overrides };
}

function makeStates() {
  return [
    {
      id: "setup-first-run", source: "live", phase: "fresh",
      enter: async (page, ctx) => {
        await openApp(page, ctx);
        await page.getByRole("button", { name: "Continue", exact: true }).waitFor({ timeout: 120_000 });
      },
    },
    {
      id: "setup-customize", source: "live", phase: "fresh",
      enter: async (page, ctx) => {
        await openApp(page, ctx);
        await page.getByRole("button", { name: "Customize", exact: true }).click({ timeout: 120_000 });
        await settle(page, 300);
      },
    },
    {
      id: "startup-checking", source: "mocked", mocked: "GET /api/setup/status never answers, holding the startup check", phase: "fresh",
      mocks: () => [{ path: /^\/api\/setup\/status$/, action: "hang" }],
      enter: async (page, ctx) => {
        await openApp(page, ctx);
        await page.getByText("Checking your local runtime…").waitFor({ timeout: 30_000 });
        await page.waitForTimeout(5_500); // the long-check explanation appears after 5 s
      },
    },
    {
      id: "startup-unreachable", source: "mocked", mocked: "every backend request is refused", phase: "fresh",
      mocks: () => [{ method: "*", path: /./, action: "abort" }],
      enter: async (page, ctx) => {
        await openApp(page, ctx);
        await page.getByText("Setup status unavailable", { exact: true }).waitFor({ timeout: 90_000 });
      },
    },
    {
      id: "library-empty", source: "live", phase: "setup",
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator("#tab-library").waitFor({ state: "visible" });
      },
    },
    {
      id: "convert-empty", source: "live", phase: "setup",
      enter: async (page, ctx) => { await openDashboard(page, ctx); await gotoTab(page, "convert"); },
    },
    {
      id: "convert-source-added", source: "live", phase: "setup",
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "convert");
        await page.locator(".manual-path summary").click();
        await page.locator("#source-path").fill(ctx.longSourceFile);
        await page.locator("#source-path").press("Enter");
        await page.locator(".queue-row.is-expanded .book-preview").waitFor({ timeout: 60_000 });
        await page.locator(".queue-row.is-expanded .book-preview summary").click();
        await parkMouse(page);
      },
    },
    {
      id: "convert-paste", source: "live", phase: "setup",
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "convert");
        await page.locator("#source-input-paste").click();
        await page.locator("#paste-title-input").fill(LONG_UNBROKEN);
        await page.locator("#paste-text-input").fill(`## ${LONG_UNBROKEN}\n\n${CJK_TITLE}\n\n${RTL_AUTHOR}`);
      },
    },
    {
      id: "settings", source: "live", phase: "backup",
      enter: async (page, ctx) => { await openDashboard(page, ctx); await gotoTab(page, "settings"); },
    },
    {
      id: "confirm-delete-model", source: "live", phase: "backup",
      overlay: { container: ".setup-model-manager .setup-model-confirm", trigger: ".setup-model-manager .setup-delete:not([disabled])" },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await openSettingsSection(page, page.locator(".setup-model-manager .setup-delete:not([disabled])").first());
        await page.locator(".setup-model-manager .setup-delete:not([disabled])").first().click();
        await page.locator(".setup-model-manager .setup-model-confirm").waitFor();
      },
    },
    {
      id: "confirm-remove-runtime", source: "live", phase: "backup",
      overlay: { container: ".setup-danger .setup-model-confirm", trigger: ".setup-danger .setup-delete" },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await openSettingsSection(page, page.locator(".setup-danger summary"));
        await page.locator(".setup-danger summary").click();
        await page.locator(".setup-danger .setup-delete").click();
        await page.locator(".setup-danger .setup-model-confirm").waitFor();
      },
    },
    {
      id: "confirm-cleanup", source: "live", phase: "backup",
      overlay: { container: ".maintenance-confirm:has(#library-cleanup-confirm-btn)", trigger: "#library-cleanup-btn" },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await openSettingsSection(page, page.locator("#library-cleanup-btn"));
        await page.locator("#library-cleanup-btn").click();
        await page.locator("#library-cleanup-confirm-btn").waitFor();
      },
    },
    {
      id: "confirm-restore-backup", source: "live", phase: "backup",
      overlay: { container: ".maintenance-confirm:has(#library-restore-confirm-btn)", trigger: ".maintenance-restore-btn" },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await openSettingsSection(page, page.locator(".maintenance-restore-btn").first());
        await page.locator(".maintenance-restore-btn").first().click();
        await page.locator("#library-restore-confirm-btn").waitFor();
      },
    },
    {
      id: "model-download-running", source: "mocked", mocked: "GET /api/setup/models/download reports a running 3B download at 37% (model-download-running wire shape)", phase: "setup",
      mocks: () => [{ path: /^\/api\/setup\/models\/download$/, json: () => downloadRunning() }],
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "settings");
        await page.locator(".setup-progress, [role=progressbar]").first().waitFor({ timeout: 30_000 });
      },
    },
    {
      id: "model-download-failed", source: "mocked", mocked: "the running download snapshot turns into the backend's network-failure message", phase: "setup",
      mocks: () => {
        let calls = 0;
        return [{ path: /^\/api\/setup\/models\/download$/, json: () => {
          calls += 1;
          return calls === 1 ? downloadRunning() : downloadFailed();
        } }];
      },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "settings");
        await page.getByText("Could not reach huggingface.co", { exact: false }).first().waitFor({ timeout: 30_000 });
      },
    },
    {
      id: "runtime-not-ready", source: "mocked", mocked: "GET /api/setup/status reports a configured runtime whose voice model went missing (state \"unavailable\", as the backend emits it)", phase: "setup",
      mocks: () => [{ path: /^\/api\/setup\/status$/, json: (real) => runtimeNotReadyStatus(real) }],
      enter: async (page, ctx) => {
        await openApp(page, ctx);
        await page.locator(".setup-frame, .setup-shell").first().waitFor({ timeout: 60_000 });
        await settle(page, 500);
      },
    },
    {
      id: "library-one-book", source: "live", phase: "oneBook",
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator("button.book-card").first().waitFor({ timeout: 30_000 });
      },
    },
    {
      id: "book-detail", source: "live", phase: "oneBook",
      enter: async (page, ctx) => { await openDashboard(page, ctx); await openFirstBook(page); },
    },
    {
      id: "confirm-delete-book", source: "live", phase: "oneBook",
      overlay: { container: ".inline-confirm:has(#library-delete-confirm-btn)", trigger: "#library-delete-btn" },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await openFirstBook(page);
        await armDeleteBook(page);
      },
    },
    {
      id: "player", source: "live", phase: "oneBook",
      enter: async (page, ctx) => { await openDashboard(page, ctx); await startPlayer(page); await gotoTab(page, "library"); },
    },
    {
      id: "now-playing", source: "live", phase: "oneBook",
      overlay: { container: "[role=dialog].np-view, [role=dialog][aria-label='Now playing']", trigger: "#player-now-playing-btn, .player-open-now-playing" },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await startPlayer(page);
        await page.locator("#player-now-playing-btn").click();
        await page.getByRole("dialog", { name: "Now playing", exact: true }).waitFor();
        await settle(page, 300);
      },
    },
    {
      id: "library-long-text", source: "live", phase: "longBooks",
      enter: async (page, ctx) => { await openDashboard(page, ctx); await page.locator("button.book-card").nth(2).waitFor({ timeout: 30_000 }); },
    },
    {
      id: "book-detail-long-text", source: "live", phase: "longBooks",
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator("button.book-card", { hasText: LONG_UNBROKEN.slice(0, 20) }).first().click();
        await page.locator(".album-title").waitFor();
        await startPlayer(page, { reuseOpenBook: true });
      },
    },
    {
      id: "book-detail-missing-author", source: "live", phase: "longBooks",
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator("button.book-card", { hasText: CJK_TITLE.slice(0, 6) }).first().click();
        await page.locator(".album-title").waitFor();
      },
    },
    {
      id: "book-detail-long-album", source: "mocked", mocked: `GET /api/library/<id> gives every book ${LONG_ALBUM_CHAPTERS} chapters (long, CJK and unbroken titles)`, phase: "longBooks",
      mocks: longAlbumMocks,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await openFirstBook(page);
        await page.locator(".chapter-row").nth(LONG_ALBUM_CHAPTERS - 1).waitFor({ state: "attached" });
        await settle(page, 300);
      },
      // Rows skip rendering off screen; every row, near or far, must keep the
      // same columns (index, title, duration) without overlapping.
      check: async (page) => {
        const columns = () => page.evaluate(() => [...document.querySelectorAll(".chapter-row")].flatMap((row) => {
          const box = row.getBoundingClientRect();
          if (box.bottom < 0 || box.top > innerHeight) return [];
          const edge = (selector) => row.querySelector(selector).getBoundingClientRect();
          const title = edge(".chapter-title");
          const duration = edge(".chapter-duration");
          return [{ layout: `${Math.round(title.left)},${Math.round(duration.right)}`, overlap: title.right > duration.left + 1 || duration.top > title.bottom }];
        }));
        const top = await columns();
        await page.locator(".chapter-row").last().scrollIntoViewIfNeeded();
        await settle(page, 200);
        const rows = [...top, ...await columns()];
        const layouts = [...new Set(rows.map((row) => row.layout))];
        return layouts.length === 1 && !rows.some((row) => row.overlap) ? [] : [{
          rule: "list-columns-misaligned", target: ".chapter-row", detail: `chapter rows use ${layouts.length} column layouts (${layouts.slice(0, 3).join(" | ")}); overlapping rows: ${rows.filter((row) => row.overlap).length}`,
        }];
      },
    },
    {
      id: "now-playing-long-album", source: "mocked", mocked: `as book-detail-long-album, with Now Playing open on the ${LONG_ALBUM_CHAPTERS}-chapter list`, phase: "longBooks",
      overlay: { container: "[role=dialog].np-view", trigger: "#player-now-playing-btn, .player-open-now-playing" },
      mocks: longAlbumMocks,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await openFirstBook(page);
        await page.locator("#player-now-playing-btn").click();
        await page.getByRole("dialog", { name: "Now playing", exact: true }).waitFor();
        await settle(page, 300);
      },
    },
    {
      id: "library-200-books", source: "mocked", mocked: "GET /api/library pages through 200 books (the real one plus 199 synthetic: long, CJK and RTL titles, missing authors, covers 404), 100 per request as the backend does, so Load more shows", phase: "longBooks",
      mocks: libraryOf200,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator("button.book-card").nth(99).waitFor({ state: "attached", timeout: 60_000 });
      },
    },
    {
      id: "library-200-books-page-2", source: "mocked", mocked: "as library-200-books, after Load more fetched the second page", phase: "longBooks",
      mocks: libraryOf200,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator("button.book-card").nth(99).waitFor({ state: "attached", timeout: 60_000 });
        await libraryLoadsSettled(page);
        await page.getByRole("button", { name: /load more/i }).click();
        await page.locator("button.book-card").nth(199).waitFor({ state: "attached", timeout: 60_000 });
      },
    },
    {
      id: "convert-running", source: "live", phase: "running",
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "convert");
        await page.locator(".queue-details-btn, #cancel-btn").first().waitFor({ timeout: 60_000 });
      },
    },
    {
      id: "library-running", source: "live", phase: "running",
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator(".rail-job").waitFor({ timeout: 60_000 });
      },
    },
    {
      id: "conversion-details", source: "live", phase: "running",
      overlay: { container: "[role=dialog][aria-label='Conversion details']", trigger: ".rail-job" },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator(".rail-job").click({ timeout: 60_000 });
        await page.getByRole("dialog", { name: "Conversion details", exact: true }).waitFor();
        await settle(page, 300);
      },
    },
    {
      id: "conversion-cancelling", source: "mocked", mocked: "the cancel request is held unanswered, so the real running job stays in Cancelling", phase: "running",
      mocks: () => [{ method: "POST", path: /^\/api\/conversions\/[^/]+\/cancel$/, action: "hang" }],
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "convert");
        await page.locator("#cancel-btn").click({ timeout: 60_000 });
        await page.locator("#cancel-confirm-stop").click({ timeout: 10_000 });
        await page.locator("#cancel-btn", { hasText: /Cancelling|Stopping/ }).waitFor({ timeout: 10_000 }).catch(() => undefined);
        await settle(page, 300);
      },
      allowRequests: [/^POST \/api\/conversions\/[^/]+\/cancel$/],
    },
    {
      id: "conversion-failed", source: "mocked", mocked: "a job seen running and then failing with a long error: GET /api/conversions/active reports it running, then failed, and the event stream replays its state, log and failure (active and SSE wire shapes)", phase: "idle",
      initScript: HOLD_COMPLETION_NOTICE,
      mocks: failedJobMocks,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "convert");
        await page.locator(".completion-notice.is-failed").waitFor({ timeout: 30_000 });
        await settle(page, 300);
      },
    },
    {
      id: "conversion-failed-details", source: "mocked", mocked: "as conversion-failed, with the details drawer opened from the failure notice on the failed job's log", phase: "idle",
      // The notice's Details button goes away as it opens the drawer, so focus
      // returns to the page heading when the drawer closes.
      overlay: { container: "[role=dialog][aria-label='Conversion details']", trigger: "#workspace-heading" },
      initScript: HOLD_COMPLETION_NOTICE,
      mocks: failedJobMocks,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "convert");
        await page.locator(".completion-notice.is-failed").getByRole("button", { name: "Details" }).click({ timeout: 30_000 });
        await page.getByRole("dialog", { name: "Conversion details", exact: true }).waitFor({ timeout: 15_000 });
        await settle(page, 300);
      },
    },
    {
      id: "backend-disconnected", source: "mocked", mocked: "after the dashboard loads, every backend request is refused", phase: "idle",
      mocks: (ctx) => {
        let refused = false;
        ctx.disconnect = () => { refused = true; };
        return [{ method: "*", path: /./, action: "abort", when: () => refused }];
      },
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator("button.book-card").first().waitFor({ timeout: 30_000 });
        ctx.disconnect();
        await page.locator(".connection-badge.is-error").first().waitFor({ timeout: 90_000 });
        await settle(page, 300);
      },
    },
    {
      id: "transition-rail-job-ends", source: "live", phase: "running", noActivation: true, noForcedColors: true,
      mocked: null,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        const job = page.locator(".rail-job");
        await job.waitFor({ timeout: 60_000 });
        // Keyboard focus and the pointer (with its tooltip) on the job button...
        await page.keyboard.press("Shift");
        await job.focus();
        const box = await job.boundingBox();
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
        await page.locator(".tooltip-bubble.is-placed").first().waitFor({ timeout: TOOLTIP_WAIT_MS }).catch(() => undefined);
        await ctx.arrive();
        // ...when the job ends and the button unmounts.
        await job.waitFor({ state: "detached", timeout: 90_000 });
        await settle(page, 400);
      },
      afterArrival: (ctx) => cancelActiveJob(ctx),
      check: async (page) => transitionFailures(page, "the job ended while its rail button had focus and its tooltip", "Show stages and the event log"),
    },
    {
      id: "transition-drawer-cancel-ends", source: "live", phase: "running", noActivation: true, noForcedColors: true,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await page.locator(".rail-job").click({ timeout: 60_000 });
        await page.getByRole("dialog", { name: "Conversion details", exact: true }).waitFor();
        const cancel = page.locator("#progress-cancel-btn");
        await cancel.waitFor({ timeout: 30_000 });
        await page.keyboard.press("Shift");
        await cancel.focus();
        await ctx.arrive();
        await cancel.waitFor({ state: "detached", timeout: 90_000 });
        await settle(page, 400);
      },
      afterArrival: (ctx) => cancelActiveJob(ctx),
      check: async (page) => transitionFailures(page, "the job ended while Cancel in the details drawer had focus", "Finished chapters are kept"),
    },
    {
      id: "transition-notice-auto-dismiss", source: "live", phase: "idle", noActivation: true, noForcedColors: true,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "convert");
        await ctx.arrive();
        const dismiss = page.locator(".completion-notice").getByRole("button", { name: "Dismiss" });
        await dismiss.waitFor({ timeout: 240_000 });
        await page.keyboard.press("Shift");
        await dismiss.focus();
        // The notice dismisses itself after ten seconds, Dismiss and all.
        await dismiss.waitFor({ state: "detached", timeout: COMPLETION_NOTICE_MS + 5_000 });
        await settle(page, 400);
      },
      afterArrival: (ctx) => startConversion(ctx.api, "A Notice That Leaves", "Mara Quinn", SHORT_TEXT),
      check: async (page) => transitionFailures(page, "the completion notice dismissed itself while its Dismiss button had focus"),
    },
    {
      id: "runtime-check-notice", source: "mocked", phase: "oneBook",
      mocked: "a returning launch: GET /api/setup/status answers \"checking\" (as the backend does while its probe runs), with the last book in the player",
      mocks: runtimeCheckMocks,
      enter: async (page, ctx) => {
        await enterRuntimeCheck(page, ctx);
        await page.getByText("Checking your local runtime", { exact: false }).first().waitFor({ timeout: 30_000 });
        await settle(page, 300);
      },
    },
    {
      id: "runtime-check-exhausted", source: "mocked", phase: "oneBook",
      mocked: "as runtime-check-notice, until the app gives up retrying (its 2 s retry delay shortened to 10 ms so the 60 retries finish quickly)",
      initScript: "(() => { const original = window.setTimeout; window.setTimeout = function (handler, delay, ...rest) { return original(handler, delay === 2000 && window.__auditFastRetries ? 10 : delay, ...rest); }; })();",
      mocks: runtimeCheckMocks,
      enter: async (page, ctx) => {
        await enterRuntimeCheck(page, ctx, { fastRetries: true });
        await page.getByText("Runtime check needs more time", { exact: false }).first().waitFor({ timeout: 90_000 });
        await settle(page, 300);
      },
    },
    {
      id: "completion-notice", source: "live", phase: "idle",
      initScript: HOLD_COMPLETION_NOTICE,
      enter: async (page, ctx) => {
        await openDashboard(page, ctx);
        await gotoTab(page, "convert");
        await ctx.arrive(); // every window is listening before the conversion starts
        await page.locator(".completion-notice").waitFor({ timeout: 240_000 });
        await settle(page, 300);
      },
      // Runs once, after every window has arrived.
      afterArrival: (ctx) => startConversion(ctx.api, "A Short Notice Book", "Mara Quinn", SHORT_TEXT),
    },
  ];
}

/**
 * Focus that fell to <body> while its control was still there. The app's focus
 * rescue puts it back at once (so the user never notices), but it means some
 * code dropped focus by mistake; the rescue reports each case and so does the audit.
 */
async function droppedFocus(page, what) {
  const rescues = await probe(page, "takeRescues");
  return rescues.filter((rescue) => rescue.kind === "refocus").map((rescue) => ({
    rule: "focus-dropped",
    target: rescue.desc,
    detail: `${what}: focus fell to <body> although ${rescue.desc} was still there (the focus rescue put it back)`,
  }));
}

/** Cancel the running conversion from outside the page, as when a job ends on its own. */
async function cancelActiveJob(ctx) {
  const active = await ctx.api("GET", "/api/conversions/active");
  if (active.active) await ctx.api("POST", `/api/conversions/${active.job.id}/cancel`, {});
}

/** After a control went away under focus: focus is somewhere usable and no tooltip outlived its anchor. */
async function transitionFailures(page, what, goneTip = null) {
  const failures = [];
  const verdict = await settledFocus(page);
  failures.push(...await droppedFocus(page, what));
  if (!verdict.ok) failures.push({ rule: "focus-lost-after-transition", target: verdict.desc, detail: `${what}: ${verdict.reason}` });
  await parkMouse(page);
  await settle(page, 200);
  // The removed control's own tip must be gone (focus may since show another).
  const tips = await page.locator(".tooltip-bubble").allTextContents();
  if (goneTip && tips.some((text) => text.includes(goneTip))) {
    failures.push({ rule: "tooltip-outlived-anchor", target: ".tooltip-bubble", detail: `${what}: "${goneTip}" is still showing after its control went away` });
  }
  return failures;
}

/** Setup status reports "checking" (with `checking: true`, as the backend does while its probe runs) once enabled. */
function runtimeCheckMocks(ctx) {
  let checking = false;
  ctx.startRuntimeCheck = () => { checking = true; };
  return [{ path: /^\/api\/setup\/status$/, when: () => checking, json: (real) => ({ ...real, state: "checking", checking: true, ready: false }) }];
}

/** A returning launch: the app remembers a ready runtime, then relaunches while the check runs. */
async function enterRuntimeCheck(page, ctx, { fastRetries = false } = {}) {
  await openDashboard(page, ctx);
  await page.locator("button.book-card").first().waitFor({ timeout: 30_000 });
  ctx.startRuntimeCheck();
  if (fastRetries) await page.addInitScript(() => { window.__auditFastRetries = true; });
  await page.reload();
  await page.locator("#tab-btn-library").waitFor({ state: "attached", timeout: 60_000 });
}

async function openFirstBook(page) {
  await page.locator("button.book-card").first().click({ timeout: 30_000 });
  await page.locator(".album-title").waitFor({ timeout: 10_000 });
  await settle(page, 300);
}

async function armDeleteBook(page) {
  await page.locator("#library-delete-btn").click();
  await page.locator("#library-delete-confirm-btn").waitFor();
  await settle(page, 200);
}

async function startPlayer(page, { reuseOpenBook = false } = {}) {
  if (!reuseOpenBook) await openFirstBook(page);
  await page.locator(".album-play").click();
  const pause = page.getByRole("button", { name: "Pause audiobook", exact: true });
  await pause.waitFor({ timeout: 30_000 });
  await pause.click();
  await settle(page, 300);
}

// ---------------------------------------------------------------------------
// Phases prepare backend data once, in state order.

function createPhases(ctx) {
  const done = new Map();
  const run = (name, fn) => {
    if (!done.has(name)) done.set(name, fn());
    return done.get(name);
  };
  const phases = {
    fresh: async () => {
      const status = await ctx.api("GET", "/api/setup/status");
      assert(!status.configured, "The isolated backend is already configured; first-run states need a fresh one");
    },
    setup: () => run("setup", async () => {
      const status = await ctx.api("GET", "/api/setup/status");
      // Mocked events must carry the real backend's instance id to be accepted.
      ctx.backendInstanceId = status.backend.instance_id;
      if (status.configured && status.ready) return;
      const context = await ctx.browser.newContext({ viewport: { width: 1440, height: 900 } });
      try {
        const page = await context.newPage();
        await openApp(page, ctx);
        await waitFor(async () => {
          if (await page.locator("#tab-btn-library").count()) return true;
          const proceed = page.getByRole("button", { name: "Continue", exact: true });
          if (await proceed.isVisible() && await proceed.isEnabled()) await proceed.click();
          return false;
        }, { timeoutMs: 180_000, message: "cached voice runtime lets setup continue" });
      } finally {
        await context.close();
      }
      log("setup complete");
    }),
    backup: () => run("backup", async () => {
      await phases.setup();
      await ctx.api("POST", "/api/maintenance/backup");
    }),
    oneBook: () => run("oneBook", async () => {
      await phases.setup();
      if ((await libraryItems(ctx.api)).length === 0) {
        log("converting the first book");
        await waitForConversion(ctx.api, await startConversion(ctx.api, "The Lighthouse Keeper", "Mara Quinn", SHORT_TEXT));
      }
      const [first] = await libraryItems(ctx.api);
      ctx.realDetail = await ctx.api("GET", `/api/library/${first.id}`);
    }),
    longBooks: () => run("longBooks", async () => {
      await phases.oneBook();
      log("converting books with long, CJK and RTL metadata");
      await waitForConversion(ctx.api, await startConversion(ctx.api, CJK_TITLE, "", SHORT_TEXT));
      await waitForConversion(ctx.api, await startConversion(ctx.api, LONG_SPACED, LONG_AUTHOR, SHORT_TEXT));
      const items = await libraryItems(ctx.api);
      const first = items.find((item) => item.title === "The Lighthouse Keeper");
      if (first) await ctx.api("PATCH", `/api/library/${first.id}`, { title: LONG_UNBROKEN, author: RTL_AUTHOR });
      const cjk = items.find((item) => item.title === CJK_TITLE);
      if (cjk) await ctx.api("PATCH", `/api/library/${cjk.id}`, { author: null });
      const refreshed = await libraryItems(ctx.api);
      ctx.realDetail = await ctx.api("GET", `/api/library/${refreshed[0].id}`);
    }),
    running: async () => {
      await phases.longBooks();
      const active = await ctx.api("GET", "/api/conversions/active");
      if (active.active) return;
      log("starting a long conversion");
      ctx.runningJobId = await startConversion(ctx.api, `Mill Stories ${LONG_SPACED}`, LONG_AUTHOR, LONG_RUNNING_TEXT);
      await waitFor(async () => (await ctx.api("GET", "/api/conversions/active")).active, { timeoutMs: 60_000, message: "long conversion running" });
    },
    idle: async () => {
      await phases.longBooks();
      const active = await ctx.api("GET", "/api/conversions/active");
      if (!active.active) return;
      await ctx.api("POST", `/api/conversions/${active.job.id}/cancel`, {});
      await waitForConversion(ctx.api, active.job.id, { allowedStatuses: ["completed", "cancelled", "failed"] });
    },
  };
  return phases;
}

// ---------------------------------------------------------------------------
// The audit of one state at one window size.

class Recorder {
  constructor(out) {
    this.out = out;
    this.failures = [];
    this.keys = new Map();
    this.screenshots = 0;
    fs.mkdirSync(path.join(out, "failures"), { recursive: true });
  }

  async add(page, base, failure) {
    const entry = { ...base, ...failure };
    const key = failureKey(entry);
    const existing = this.keys.get(key);
    if (existing) {
      existing.occurrences += 1;
      return;
    }
    entry.occurrences = 1;
    this.keys.set(key, entry);
    this.failures.push(entry);
    if (page && this.screenshots < MAX_SCREENSHOTS) {
      this.screenshots += 1;
      const name = `${String(this.screenshots).padStart(3, "0")}-${entry.state}-${entry.viewport}-${entry.rule}.png`;
      try {
        if (failure.rect) await probe(page, "mark", null, failure.rect);
        await page.screenshot({ path: path.join(this.out, "failures", name) });
        entry.screenshot = path.join("failures", name);
      } catch {
        // A page that already closed cannot be photographed; the entry stays.
      } finally {
        await probe(page, "unmark").catch(() => undefined);
      }
    }
  }
}

// Ellipsized text without a title passes only if hovering it really shows a
// tooltip with the full value (the shared tooltip layer provides one).
async function withoutRevealedText(page, failures) {
  const kept = [];
  for (const failure of failures) {
    if (failure.rule !== "truncated-text-unreadable" || !failure.textId) {
      kept.push(failure);
      continue;
    }
    const point = await probe(page, "reveal", failure.textId);
    let shown = null;
    if (point) {
      await page.mouse.move(point.x, point.y);
      const wanted = failure.fullText.slice(0, 80);
      await page.waitForFunction((text) => document.querySelector(".tooltip-bubble.is-placed")?.textContent.replace(/\s+/g, " ").includes(text), wanted, { timeout: TOOLTIP_WAIT_MS }).catch(() => undefined);
      shown = await probe(page, "tooltipText");
      await parkMouse(page);
    }
    if (shown && shown.includes(failure.fullText.slice(0, 80))) continue;
    kept.push({ ...failure, detail: `${failure.detail}; hovering it shows ${shown ? `"${shown.slice(0, 80)}"` : "no tooltip"}` });
  }
  await settle(page);
  return kept;
}

async function auditPage(page, { state, viewportLabel, recorder, stats, shared }) {
  const base = { state: state.id, viewport: viewportLabel };
  const record = async (phase, failures, action) => {
    for (const failure of failures) await recorder.add(page, { ...base, phase, action }, failure);
  };
  await probe(page, "disableTransitions");
  await parkMouse(page);
  await settle(page, 200);

  // 1. Static geometry of the state as entered.
  const snapshot = await probe(page, "snapshot");
  stats.elements += snapshot.elements.length;
  await record("static", await withoutRevealedText(page, checkSnapshot(snapshot)));

  // 2. Scroll every scroll container to its end: the last rows must not stay
  //    under a fixed or sticky layer such as the player bar.
  for (const container of await probe(page, "scrollContainers")) {
    const original = container.scrollTop;
    await probe(page, "scrollTo", container.id, "end");
    await settle(page);
    for (const element of await probe(page, "factsWithin", container.id)) {
      if (element.hidden) continue;
      await record("scrolled-to-end", [...checkClipping(element), ...checkCoverage(element)], container.desc);
    }
    await probe(page, "scrollTo", container.id, original);
  }
  await settle(page);

  // 3. Hover every interactive element with the real mouse.
  // With focus left inside a container that handles Escape (the sidebar
  // collapses on it), the tooltip Escape check would act on that container.
  if (!state.overlay) await probe(page, "blurActive");
  await probe(page, "captureFocusBaseline");
  let escapeChecked = Boolean(state.overlay);
  const tipWaits = new Map();
  for (const element of await probe(page, "elements")) {
    if (element.hidden || !(await probe(page, "exists", element.id))) continue;
    const point = await probe(page, "reveal", element.id);
    if (!point) continue;
    await settle(page);
    const before = await probe(page, "layout");
    await page.mouse.move(point.x, point.y);
    stats.hovers += 1;
    let tooltip = null;
    const tipKind = `${element.signature}|${element.tip}`;
    const viewport = page.viewportSize();
    const nearEdge = point.rect.x < 160 || point.rect.y < 160
      || point.rect.x + point.rect.w > viewport.width - 160 || point.rect.y + point.rect.h > viewport.height - 160;
    const waitForTip = element.tip && (nearEdge || (tipWaits.get(tipKind) ?? 0) < TOOLTIP_WAITS_PER_KIND);
    if (waitForTip) {
      tipWaits.set(tipKind, (tipWaits.get(tipKind) ?? 0) + 1);
      tooltip = await page.locator(".tooltip-bubble.is-placed").first().waitFor({ timeout: TOOLTIP_WAIT_MS }).then(() => true, () => false);
      if (!tooltip && !element.disabled) {
        await record("hover", [{ rule: "tooltip-not-shown", target: element.desc, detail: `hovering for ${TOOLTIP_WAIT_MS}ms did not show "${element.tip}"`, rect: element.rect }]);
      }
    } else {
      await settle(page);
    }
    const during = await probe(page, "layout");
    const page_ = await probe(page, "page");
    const failures = [...checkHorizontalScroll(page_)];
    for (const popup of await probe(page, "popups")) {
      failures.push(...(popup.tooltip ? checkTooltip(popup, point.rect, page_.viewport) : checkFloatingInViewport(popup, page_.viewport)));
      if (popup.chain.length) failures.push(...checkClipping(popup, { strict: true, rule: "floating-clipped" }));
    }
    if (tooltip && !escapeChecked) {
      escapeChecked = true;
      await page.keyboard.press("Escape");
      await settle(page);
      if (await page.locator(".tooltip-bubble").count()) failures.push({ rule: "escape-does-not-close", target: element.desc, detail: "Escape left the hover tooltip open" });
    }
    await parkMouse(page);
    await settle(page);
    const restored = await probe(page, "layout");
    const ignore = new Set([element.id]);
    let shifts = layoutShifts(before, during, restored, { ignore });
    if (shifts.length) {
      // Live content (a running conversion's stage list) can change and change
      // back within one hover. A shift counts only if a second hover repeats it.
      const again = { before: await probe(page, "layout") };
      await page.mouse.move(point.x, point.y);
      if (waitForTip) await page.locator(".tooltip-bubble.is-placed").first().waitFor({ timeout: TOOLTIP_WAIT_MS }).catch(() => undefined);
      else await settle(page);
      again.during = await probe(page, "layout");
      await parkMouse(page);
      await settle(page);
      const repeated = layoutShifts(again.before, again.during, await probe(page, "layout"), { ignore });
      const sameMove = (a, b) => ["x", "y", "w", "h"].every((key) => Math.abs((a.during[key] - a.before[key]) - (b.during[key] - b.before[key])) <= 0.5);
      shifts = shifts.filter((shift) => repeated.some((other) => other.id === shift.id && sameMove(shift, other)));
    }
    for (const shift of shifts) {
      failures.push({ rule: "layout-shift-on-hover", target: await probe(page, "describeId", shift.id), detail: `moved from ${JSON.stringify(shift.before)} to ${JSON.stringify(shift.during)} while hovering ${element.desc}`, rect: shift.during });
    }
    await record("hover", failures, element.desc);
  }

  // 4. Walk the page with Tab; open every disclosure, popup and dialog.
  await tabTraversal(page, { state, record, stats, shared, viewportLabel });
}

async function startTabWalk(page) {
  await page.evaluate(() => {
    const start = document.createElement("span");
    start.tabIndex = -1;
    start.setAttribute("data-interaction-start", "");
    Object.assign(start.style, { position: "fixed", left: "0", top: "0", width: "0", height: "0" });
    const dialog = [...document.querySelectorAll("[role=dialog][aria-modal=true],[role=alertdialog]")].find((element) => element.checkVisibility());
    (dialog ?? document.body).prepend(start);
    start.focus();
  });
  await page.keyboard.press("Tab");
  await page.evaluate(() => document.querySelector("[data-interaction-start]")?.remove());
}

async function tabTraversal(page, { state, record, stats, shared, viewportLabel }) {
  await parkMouse(page);
  await probe(page, "blurActive");
  await settle(page);
  await startTabWalk(page);
  const pageInfo = await probe(page, "page");
  const visited = new Set();
  const openedKinds = new Map();
  const openedIds = new Set();
  let firstId = null;
  let previousId = null;
  let repeats = 0;
  // Opening something can move focus on its own (a toggle handing focus into
  // the region it shows); that element is the next stop, without a Tab.
  let focusMovedByOpen = false;
  const maxSteps = (await probe(page, "elements")).length * 2 + 20;
  for (let step = 0; step < maxSteps; step += 1) {
    if (step > 0 && !focusMovedByOpen) await page.keyboard.press("Tab");
    focusMovedByOpen = false;
    await settle(page);
    let info = await probe(page, "focusInfo");
    if (info.none) {
      if (step > 0 && previousId === null) break; // left the document twice: done
      previousId = null;
      continue;
    }
    if (info.id === firstId && step > 0) break;
    if (firstId === null) firstId = info.id;
    if (info.id === previousId) {
      repeats += 1;
      if (repeats >= 2 && !info.inDialog) {
        await record("focus", [{ rule: "focus-trapped", target: info.desc, detail: "Tab does not move focus away from this control", rect: info.rect }]);
        break;
      }
    } else repeats = 0;
    previousId = info.id;
    if (visited.has(info.id) && !info.inDialog) continue;
    visited.add(info.id);
    stats.focusStops += 1;
    stats.tabOrder.push(info.desc);
    // Ellipsized text inside the focused control must be readable by keyboard too.
    const cutShort = info.tip ? [] : await probe(page, "cutShortTexts", info.id);
    if (info.tip || cutShort.length) {
      await page.locator(".tooltip-bubble.is-placed").first().waitFor({ timeout: 400 }).catch(() => undefined);
      info = await probe(page, "focusInfo");
    }
    const failures = [...checkFocusLanding(info, pageInfo.viewport), ...checkFocusVisible(info)];
    // Measure the indicator in pixels once per kind of control and window size.
    if (!info.hidden && !info.none) {
      const indicator = await probe(page, "indicatorBox");
      const key = indicator && `${viewportLabel}|${indicator.key}`;
      if (key && !shared.indicators.has(key)) {
        const measure = await measureFocusIndicator(page, probe, info.id);
        shared.indicators.set(key, measure);
        failures.push(...checkFocusIndicator(measure));
        stats.indicatorsMeasured += 1;
      }
    }
    if (cutShort.length) {
      const shown = await probe(page, "tooltipText") ?? "";
      const hidden = cutShort.filter((text) => !shown.includes(text.slice(0, 80)));
      if (hidden.length) failures.push({ rule: "truncated-text-unreadable", target: info.desc, detail: `keyboard focus does not reveal the full text: "${hidden[0].slice(0, 120)}"`, rect: info.rect });
    }
    for (const popup of await probe(page, "popups")) {
      failures.push(...(popup.tooltip ? checkTooltip(popup, info.tipAnchor ?? info.rect, pageInfo.viewport) : checkFloatingInViewport(popup, pageInfo.viewport)));
      if (popup.chain.length) failures.push(...checkClipping(popup, { strict: true, rule: "floating-clipped" }));
    }
    await record("focus", failures, info.desc);

    const opener = info.tag === "summary" || (info.hasPopup && info.hasPopup !== "false") || info.expanded !== null;
    if (opener && !openedIds.has(info.id)) {
      const count = openedKinds.get(info.signature) ?? 0;
      if (count < OPEN_TESTS_PER_KIND) {
        openedKinds.set(info.signature, count + 1);
        openedIds.add(info.id);
        stats.opens += 1;
        const controlsBefore = await probe(page, "elements");
        await openAndClose(page, info, { record, viewport: pageInfo.viewport });
        const activeId = await probe(page, "activeId");
        focusMovedByOpen = activeId !== info.id;
        if (focusMovedByOpen) {
          // A toggle that closes and reopens a dialog gets it back built from
          // new nodes, with focus at its top. The copies of controls already
          // walked count as walked, and the walk goes on from the toggle
          // rather than through the whole dialog once more per toggle.
          const { twins, unmatched } = rebuiltTwins(controlsBefore, await probe(page, "elements"));
          for (const [copy, original] of twins) {
            if (visited.has(original)) visited.add(copy);
            if (original === firstId) firstId = copy;
          }
          if (twins.has(activeId) && visited.has(activeId) && unmatched.length === 0 && await probe(page, "focusId", info.id)) {
            focusMovedByOpen = false;
          }
        }
      }
    }
  }
  // Every focusable control should be reachable with Tab.
  const missed = [];
  for (const element of await probe(page, "snapshot").then((snapshot) => snapshot.elements)) {
    if (!element.focusable || element.hidden || element.inert || element.disabled || visited.has(element.id)) continue;
    if (element.type === "radio") continue; // a radio group is one Tab stop; arrows move within it
    missed.push({ rule: "not-reachable-by-tab", target: element.desc, detail: "a focusable control that Tab never reaches", rect: element.rect });
  }
  await record("focus", missed);
}

async function openAndClose(page, trigger, { record, viewport }) {
  const kind = trigger.tag === "summary" ? "details" : trigger.hasPopup && trigger.hasPopup !== "false" ? "popup" : "expanded";
  const action = `open ${trigger.desc}`;
  // A toggletip that opens on focus is already open: check it, then Escape.
  const shownOnFocus = trigger.expanded === "true" && trigger.controls
    ? (await probe(page, "popups")).find((popup) => popup.popup && popup.tooltip && popup.domId === trigger.controls)
    : null;
  if (shownOnFocus) {
    const failures = [...checkFloatingInViewport(shownOnFocus, viewport)];
    if (shownOnFocus.chain.length) failures.push(...checkClipping(shownOnFocus, { strict: true, rule: "floating-clipped" }));
    if (shownOnFocus.tooltip) failures.push(...checkTooltip(shownOnFocus, trigger.rect, viewport).filter((failure) => failure.rule === "tooltip-covers-anchor"));
    await page.keyboard.press("Escape");
    await settle(page, 150);
    if ((await probe(page, "popups")).some((popup) => popup.id === shownOnFocus.id)) {
      failures.push({ rule: "escape-does-not-close", target: trigger.desc, detail: `Escape left ${shownOnFocus.desc} open` });
    }
    if ((await probe(page, "activeId")) !== trigger.id) {
      failures.push({ rule: "focus-not-returned", target: trigger.desc, detail: "Escape moved focus away from the trigger" });
    }
    await record("open", failures, action);
    return;
  }
  const initial = await probe(page, "disclosureState", trigger.id);
  const dialogsBefore = new Set((await probe(page, "dialogs")).map((dialog) => dialog.id));
  const before = await probe(page, "layout");
  await page.keyboard.press("Enter");
  await settle(page, 150);
  const opened = await probe(page, "disclosureState", trigger.id);
  const newDialogs = (await probe(page, "dialogs")).filter((dialog) => !dialogsBefore.has(dialog.id));
  const popupsNow = await probe(page, "popups");
  const changed = kind === "details" ? opened.detailsOpen !== initial.detailsOpen
    : opened.connected === false || opened.visible === false || opened.expanded !== initial.expanded || newDialogs.length > 0;
  const failures = [];
  if (!changed) {
    await record("open", [{ rule: "disclosure-did-not-open", target: trigger.desc, detail: `Enter did not change its ${kind === "details" ? "open state" : "aria-expanded"}`, rect: trigger.rect }], action);
    return;
  }
  const overlay = kind === "popup" || newDialogs.length > 0;
  const during = await probe(page, "layout");
  failures.push(...checkHorizontalScroll(await probe(page, "page")));
  for (const popup of popupsNow) {
    failures.push(...checkFloatingInViewport(popup, viewport));
    if (popup.chain.length) failures.push(...checkClipping(popup, { strict: true, rule: "floating-clipped" }));
  }
  // Where did focus go?
  const activeId = await probe(page, "activeId");
  const focusInfo = await probe(page, "focusInfo");
  if (newDialogs.length) {
    if (!(await probe(page, "focusedWithin", newDialogs[0].id))) {
      failures.push({ rule: "focus-not-moved-into-dialog", target: trigger.desc, detail: `opening ${newDialogs[0].desc} left focus on ${focusInfo.desc}` });
    } else {
      failures.push(...checkFocusVisible(focusInfo).map((failure) => ({ ...failure, detail: `${failure.detail} (initial focus in ${newDialogs[0].desc})` })));
      if (newDialogs[0].modal) failures.push(...await checkFocusContained(page, newDialogs[0]));
    }
  } else if (kind === "popup") {
    const inPopup = popupsNow.some((popup) => popup.popup && !popup.tooltip);
    if (!inPopup) failures.push({ rule: "popup-not-visible", target: trigger.desc, detail: "aria-expanded flipped but no popup is visible" });
    if (activeId === null) failures.push({ rule: "focus-lost-on-open", target: trigger.desc, detail: "focus fell back to the page when the popup opened" });
  } else if (activeId === null) {
    failures.push({ rule: "focus-lost-on-open", target: trigger.desc, detail: "focus fell back to the page when the disclosure opened" });
  }
  // Close it again.
  const dialogsBeforeClose = new Set((await probe(page, "dialogs")).map((dialog) => dialog.id));
  // A disclosure that moved focus into what it shows (the compact sidebar) is
  // closed from there with Escape; Enter would activate whatever has focus.
  const focusMovedIn = !overlay && trigger.controls && activeId !== trigger.id
    && await page.evaluate(([id]) => Boolean(document.getElementById(id)?.contains(document.activeElement)), [trigger.controls]);
  if (overlay || focusMovedIn) await page.keyboard.press("Escape");
  else await page.keyboard.press("Enter");
  await settle(page, 150);
  failures.push(...await droppedFocus(page, `closing what ${trigger.desc} opened`));
  const closed = await probe(page, "disclosureState", trigger.id);
  const stillOpen = kind === "details" ? closed.detailsOpen !== initial.detailsOpen
    : (await probe(page, "dialogs")).some((dialog) => newDialogs.some((created) => created.id === dialog.id)) || (closed.connected && closed.expanded !== initial.expanded);
  if (stillOpen) {
    failures.push({ rule: overlay || focusMovedIn ? "escape-does-not-close" : "disclosure-does-not-close", target: trigger.desc, detail: overlay || focusMovedIn ? `Escape left ${overlay ? "the overlay" : "what it shows"} open` : "Enter on the trigger did not close it again", rect: trigger.rect });
  }
  const after = await probe(page, "activeId");
  if (after !== trigger.id) {
    // A toggle that hides itself hands over to its twin (same aria-controls),
    // which may in turn move focus into the region it shows.
    const controls = trigger.controls;
    const afterInfo = await probe(page, "focusInfo");
    const handedOver = controls && closed.visible !== true && afterInfo.controls === controls;
    const insideRegion = controls && await page.evaluate(([id]) => Boolean(document.getElementById(id)?.contains(document.activeElement)), [controls]);
    // Toggling a dialog's trigger back on reopens the dialog, which takes focus.
    const reopened = (await probe(page, "dialogs")).filter((dialog) => !dialogsBeforeClose.has(dialog.id));
    let insideReopened = false;
    for (const dialog of reopened) insideReopened ||= await probe(page, "focusedWithin", dialog.id);
    if (!handedOver && !insideRegion && !insideReopened) {
      failures.push({ rule: "focus-not-returned", target: trigger.desc, detail: `after closing, focus is on ${afterInfo.desc} instead of the trigger`, rect: trigger.rect });
    }
  }
  if (overlay) {
    // An overlay must not move the page beneath it.
    const restored = await probe(page, "layout");
    for (const shift of layoutShifts(before, during, restored, { ignore: new Set([trigger.id]) })) {
      failures.push({ rule: "layout-shift-on-open", target: await probe(page, "describeId", shift.id), detail: `moved from ${JSON.stringify(shift.before)} to ${JSON.stringify(shift.during)} while ${trigger.desc} was open`, rect: shift.during });
    }
  }
  await record("open", failures, action);
}

async function checkFocusContained(page, dialog) {
  const failures = [];
  for (let step = 0; step < 25; step += 1) {
    await page.keyboard.press(step % 7 === 6 ? "Shift+Tab" : "Tab");
    await settle(page);
    if (!(await probe(page, "focusedWithin", dialog.id))) {
      const info = await probe(page, "focusInfo");
      failures.push({ rule: "focus-escapes-modal", target: dialog.desc, detail: `Tab moved focus out of the modal to ${info.desc}` });
      break;
    }
  }
  return failures;
}

// Every way out of an overlay must close it and return focus to its trigger:
// Escape, its own Close/Cancel/Keep button, and a click on its backdrop where
// it has one. Each is tried on a freshly reopened overlay.
const CLOSE_NAMES = /^(close|cancel|keep)/i;

async function closedWithFocusReturned(page, state, container, how) {
  await settle(page, 250);
  const dropped = await droppedFocus(page, `closing with ${how}`);
  if (dropped.length) return dropped;
  if (await container.isVisible()) {
    return [{ rule: how === "Escape" ? "escape-does-not-close" : "overlay-does-not-close", target: state.overlay.container, detail: `${how} left the overlay open` }];
  }
  const returned = await page.evaluate((selector) => {
    const active = document.activeElement;
    return [...document.querySelectorAll(selector)].some((trigger) => trigger === active);
  }, state.overlay.trigger);
  if (returned) return [];
  const info = await probe(page, "focusInfo");
  return [{ rule: "focus-not-returned", target: state.overlay.trigger, detail: `after closing with ${how}, focus is on ${info.desc} instead of the trigger` }];
}

async function reopenOverlay(page, state, container) {
  const trigger = page.locator(state.overlay.trigger).filter({ visible: true }).first();
  if (!await trigger.count()) return false;
  await page.keyboard.press("Shift"); // keyboard modality, as a keyboard user reopens it
  await trigger.focus();
  await page.keyboard.press("Enter");
  return container.waitFor({ state: "visible", timeout: 5_000 }).then(() => true, () => false);
}

async function checkOverlayClose(page, state, record) {
  const container = page.locator(state.overlay.container).first();
  if (!await container.isVisible()) return;
  // Put focus back inside the overlay the way a user would: on its first control.
  const first = container.locator("button:not([disabled]), [href], input, [tabindex]:not([tabindex='-1'])").first();
  if (await first.count()) await first.focus();
  await page.keyboard.press("Escape");
  await record("overlay-close", await closedWithFocusReturned(page, state, container, "Escape"), "Escape");

  if (await reopenOverlay(page, state, container)) {
    const buttons = await container.locator("button:not([disabled])").all();
    let closeButton = null;
    for (const button of buttons) {
      const name = (await button.getAttribute("aria-label")) ?? (await button.textContent()) ?? "";
      if (CLOSE_NAMES.test(name.trim())) {
        closeButton = button;
        break;
      }
    }
    if (closeButton) {
      const name = ((await closeButton.getAttribute("aria-label")) ?? (await closeButton.textContent()) ?? "").trim();
      await closeButton.focus();
      await page.keyboard.press("Enter");
      await record("overlay-close", await closedWithFocusReturned(page, state, container, `its "${name}" button`), `"${name}" button`);
    }
  }

  const backdrop = page.locator("[class*=backdrop]").filter({ visible: true }).first();
  if (await reopenOverlay(page, state, container) && await backdrop.count()) {
    const box = await backdrop.boundingBox();
    await page.mouse.click(box.x + 8, box.y + box.height / 2);
    await record("overlay-close", await closedWithFocusReturned(page, state, container, "a click on its backdrop"), "backdrop click");
  }
}

async function checkOverlayInitialFocus(page, state, record) {
  const inside = await page.evaluate((selector) => {
    const container = document.querySelector(selector);
    return Boolean(container && container.contains(document.activeElement));
  }, state.overlay.container);
  if (!inside) {
    const info = await probe(page, "focusInfo");
    await record("overlay-open", [{ rule: "focus-not-moved-into-overlay", target: state.overlay.container, detail: `after opening, focus is on ${info.desc}` }], "open");
  }
}

// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Passes run once per state at one window size (they re-enter the state, so
// running them at every size would multiply the run time without new cases).

const PASS_VIEWPORT = Object.freeze({ width: 1440, height: 900 });
const ACTIVATIONS_PER_STATE = 30;
const FOCUS_SETTLE_MS = 2_500;

/** A fresh window in `state`: probe, init script, routes and the state's own entry. */
async function openStateSession(ctx, state, viewport, { guard = { activating: false }, contextOptions = {} } = {}) {
  const context = await ctx.browser.newContext({ viewport, reducedMotion: "reduce", deviceScaleFactor: 1, ...contextOptions });
  const guarded = [];
  try {
    await context.addInitScript(installInteractionProbe);
    if (state.initScript) await context.addInitScript({ content: state.initScript });
    // A single window "arrives" alone: start whatever the state waits for now.
    const stateCtx = { ...ctx, arrive: async () => { if (state.afterArrival) await state.afterArrival(ctx); } };
    await installRoutes(context, stateCtx, state, (request) => guarded.push(request), guard);
    const sessionPage = await context.newPage();
    await state.enter(sessionPage, stateCtx);
    await probe(sessionPage, "disableTransitions");
    await parkMouse(sessionPage);
    await settle(sessionPage, 200);
    return { context, page: sessionPage, guarded };
  } catch (error) {
    await context.close().catch(() => undefined);
    throw error;
  }
}

/** Wait out a transition (a busy control, a removed row) until focus settles somewhere usable. */
async function settledFocus(page, timeoutMs = FOCUS_SETTLE_MS) {
  const deadline = Date.now() + timeoutMs;
  let verdict = await probe(page, "focusSanity");
  while (!verdict.ok && Date.now() < deadline) {
    await page.waitForTimeout(100);
    verdict = await probe(page, "focusSanity");
  }
  return verdict;
}

/**
 * Press every ordinary button with Enter, and again with Space if it is still
 * there, and check where focus lands once the result has rendered: never on
 * <body>, never on something invisible or off screen, never outside an open
 * modal; also when the button disabled itself, unmounted or changed the view.
 * Requests that would change data are answered with mocked successes, so the
 * transition happens without touching the backend. Each kind of button is
 * pressed once per run; a press that changes the view reopens the state.
 */
async function activationPass(ctx, state, recorder) {
  if (state.noActivation) return { activations: 0 };
  const label = `${PASS_VIEWPORT.width}x${PASS_VIEWPORT.height}`;
  const guard = { activating: true };
  let session = await openStateSession(ctx, state, PASS_VIEWPORT, { guard });
  let activations = 0;
  const record = async (failures, action) => {
    for (const failure of failures) await recorder.add(session.page, { state: state.id, viewport: label, phase: "activation", action }, failure);
  };
  try {
    const targets = await probe(session.page, "activationTargets");
    for (const target of targets) {
      if (activations >= ACTIVATIONS_PER_STATE || ctx.shared.activated.has(target.key)) continue;
      ctx.shared.activated.add(target.key);
      let current = (await probe(session.page, "activationTargets")).find((candidate) => candidate.key === target.key);
      if (!current) continue;
      for (const key of ["Enter", " "]) {
        const keyName = key === " " ? "Space" : key;
        if (!await probe(session.page, "usable", current.id)) break;
        const before = await probe(session.page, "viewFingerprint");
        await session.page.keyboard.press("Shift"); // keyboard modality, as for a keyboard user
        if (!await probe(session.page, "focusId", current.id)) break;
        await session.page.keyboard.press(key);
        activations += 1;
        await settle(session.page, 150);
        const verdict = await settledFocus(session.page);
        await record(await droppedFocus(session.page, `${keyName} on ${target.desc}`), `${keyName} ${target.desc}`);
        if (!verdict.ok) {
          await record([{ rule: "focus-lost-after-activation", target: target.desc, detail: `${keyName} on ${target.desc}: ${verdict.reason} (${verdict.desc})` }], `${keyName} ${target.desc}`);
        }
        const changedView = (await probe(session.page, "viewFingerprint")) !== before;
        if (changedView) {
          await session.context.close().catch(() => undefined);
          try {
            session = await openStateSession(ctx, state, PASS_VIEWPORT, { guard });
          } catch (error) {
            error.stack = `reopening the state after ${keyName} on ${target.desc}: ${error.stack ?? error.message}`;
            throw error;
          }
          break;
        }
        current = (await probe(session.page, "activationTargets")).find((candidate) => candidate.key === target.key);
        if (!current) break;
      }
    }
  } finally {
    await session.context.close().catch(() => undefined);
  }
  return { activations };
}

/**
 * Windows High Contrast, dark and light: every labelled control's text is
 * drawn, and every selected or current control looks different from its
 * unselected siblings. Each kind of control is measured once per scheme.
 */
async function forcedColorsPass(ctx, state, recorder) {
  if (state.noForcedColors) return { labels: 0, states: 0 };
  let labels = 0;
  let pairs = 0;
  for (const scheme of ["dark", "light"]) {
    const label = `${PASS_VIEWPORT.width}x${PASS_VIEWPORT.height} forced-${scheme}`;
    const session = await openStateSession(ctx, state, PASS_VIEWPORT, { contextOptions: { forcedColors: "active", colorScheme: scheme } });
    try {
      const record = async (failures, action) => {
        for (const failure of failures) await recorder.add(session.page, { state: state.id, viewport: label, phase: "forced-colors", action }, failure);
      };
      for (const control of await probe(session.page, "labelledControls")) {
        const key = `${scheme}|${control.key}`;
        if (ctx.shared.forcedSeen.has(key)) continue;
        ctx.shared.forcedSeen.add(key);
        labels += 1;
        await record(checkForcedLabel(await measureLabelVisibility(session.page, probe, control.id), control.desc, scheme), control.desc);
      }
      for (const pair of await probe(session.page, "selectedPairs")) {
        const key = `${scheme}|state|${pair.key}`;
        if (ctx.shared.forcedSeen.has(key)) continue;
        ctx.shared.forcedSeen.add(key);
        pairs += 1;
        await record(checkForcedState(await measureStateDistinct(session.page, probe, pair.selected, pair.plain), pair.desc, scheme), pair.desc);
      }
    } finally {
      await session.context.close().catch(() => undefined);
    }
  }
  return { labels, states: pairs };
}

async function runState(ctx, state, viewports, recorder, options = {}) {
  const started = Date.now();
  const result = { id: state.id, source: state.source, mocked: state.mocked ?? null, viewports: {} };
  let arrived = 0;
  let releaseArrival;
  const allArrived = new Promise((resolve) => { releaseArrival = resolve; });
  const arrive = () => {
    arrived += 1;
    if (arrived === viewports.length) {
      releaseArrival();
      if (state.afterArrival) state.afterArrival(ctx).catch((error) => log(`${state.id}: ${error.message}`));
    }
    return allArrived;
  };
  const workers = viewports.map(async (viewport) => {
    const label = `${viewport.width}x${viewport.height}`;
    const stats = { elements: 0, hovers: 0, focusStops: 0, opens: 0, indicatorsMeasured: 0, tabOrder: [] };
    const context = await ctx.browser.newContext({ viewport, reducedMotion: "reduce", deviceScaleFactor: 1 });
    let page;
    const guarded = [];
    const pageErrors = [];
    try {
      await context.addInitScript(installInteractionProbe);
      if (state.initScript) await context.addInitScript({ content: state.initScript });
      const stateCtx = { ...ctx, arrive };
      await installRoutes(context, stateCtx, state, (request) => guarded.push(request));
      page = await context.newPage();
      page.on("pageerror", (error) => pageErrors.push(String(error.message ?? error)));
      await state.enter(page, stateCtx);
      fs.mkdirSync(path.join(recorder.out, "states"), { recursive: true });
      await page.screenshot({ path: path.join(recorder.out, "states", `${state.id}-${label}.png`) });
      const base = { state: state.id, viewport: label };
      const record = async (phase, failures, action) => {
        for (const failure of failures) await recorder.add(page, { ...base, phase, action }, failure);
      };
      if (state.overlay) await checkOverlayInitialFocus(page, state, record);
      await auditPage(page, { state, viewportLabel: label, recorder, stats, shared: ctx.shared });
      if (state.overlay) await checkOverlayClose(page, state, record);
      if (state.check) await record("state", await state.check(page));
      await record("page", await droppedFocus(page, "during the audit"));
      await record("page", [
        ...pageErrors.map((message) => ({ rule: "page-error", target: "page", detail: message })),
        ...guarded.map((request) => ({ rule: "unexpected-destructive-request", target: request, detail: "the audit's hover/focus/open actions sent a destructive request (blocked)" })),
      ]);
    } catch (error) {
      await recorder.add(page ?? null, { state: state.id, viewport: label, phase: "harness" }, { rule: "state-could-not-be-audited", target: state.id, detail: redactSecrets(error.stack ?? String(error), [ctx.backend.token])
        + (guarded.length ? `
Blocked requests: ${guarded.join(", ")}` : "") + (pageErrors.length ? `
Page errors: ${pageErrors.join(" | ")}` : "") });
    } finally {
      await context.close().catch(() => undefined);
    }
    result.viewports[label] = { ...stats, failures: recorder.failures.filter((failure) => failure.state === state.id && failure.viewport === label).length };
  });
  await Promise.all(workers);
  if (!options.skipPasses) {
    for (const [name, pass] of [["activation", activationPass], ["forcedColors", forcedColorsPass]]) {
      try {
        result[name] = await pass(ctx, state, recorder);
      } catch (error) {
        await recorder.add(null, { state: state.id, viewport: `${PASS_VIEWPORT.width}x${PASS_VIEWPORT.height}`, phase: name }, { rule: "state-could-not-be-audited", target: state.id, detail: redactSecrets(error.stack ?? String(error), [ctx.backend.token]) });
      }
    }
  }
  result.seconds = Math.round((Date.now() - started) / 1000);
  // One console line per distinct defect, with every window size it showed at.
  const grouped = new Map();
  for (const failure of recorder.failures.filter((entry) => entry.state === state.id)) {
    const key = `${failure.rule}|${failure.target}`;
    const entry = grouped.get(key) ?? { failure, viewports: [] };
    entry.viewports.push(failure.viewport);
    grouped.set(key, entry);
  }
  for (const { failure, viewports: sizes } of grouped.values()) {
    console.log(`  FAIL ${failure.rule} - ${failure.target} [${sizes.join(", ")}] ${failure.phase}${failure.action ? ` (${failure.action})` : ""}: ${failure.detail.split(/\r?\n/)[0]}`);
  }
  return result;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  const states = makeStates();
  if (options["list-states"]) {
    for (const state of states) console.log(`${state.id.padEnd(28)} ${state.source}${state.mocked ? `: ${state.mocked}` : ""}`);
    return 0;
  }
  // --quick: the smallest and largest window only, and no once-per-state passes.
  const viewports = selectViewports(options.viewport ?? (options.quick ? QUICK_VIEWPORTS : undefined));
  const selected = selectStates(options.state, states.map((state) => state.id));
  const out = path.resolve(typeof options.out === "string" ? options.out : path.join(repoRoot, ".artifacts", "interaction-audit"));
  fs.rmSync(path.join(out, "failures"), { recursive: true, force: true });
  fs.mkdirSync(out, { recursive: true });
  const stateDir = path.join(here, ".state", `interaction-${Date.now()}`);
  fs.mkdirSync(stateDir, { recursive: true });
  const recorder = new Recorder(out);
  const report = { startedAt: new Date().toISOString(), viewports: viewports.map((v) => `${v.width}x${v.height}`), states: [], failures: recorder.failures };
  const writeReport = () => fs.writeFileSync(path.join(out, "report.json"), JSON.stringify(report, null, 2) + "\n");
  try {
    const backend = await startBackend({ repoRoot, stateDir, log });
    // Isolation proof: the backend's storage must live in this run's folder.
    await waitFor(() => fs.existsSync(backend.dbPath), { timeoutMs: 30_000, message: `database created at ${backend.dbPath}` });
    const frontend = await startFrontend({ desktopDir: path.join(repoRoot, "desktop"), port: 5173, log });
    const browser = await launchChromium(chromium, { headless: !options.headed, args: ["--host-resolver-rules=MAP localhost 127.0.0.1", "--mute-audio"] });
    registerCleanup(() => browser.close());
    const ctx = { backend, frontend, browser, api: createApi(backend), shared: { indicators: new Map(), activated: new Set(), forcedSeen: new Set() } };
    report.browser = browser.version();
    report.isolatedStateDir = stateDir;
    ctx.longSourceFile = path.join(stateDir, `${LONG_UNBROKEN.slice(0, 90)} – ${CJK_TITLE.slice(0, 12)}.md`);
    fs.writeFileSync(ctx.longSourceFile, [`# ${LONG_UNBROKEN}`, "", ...Array.from({ length: 14 }, (_, index) => `## ${index % 3 === 0 ? LONG_UNBROKEN : index % 3 === 1 ? CJK_TITLE : LONG_SPACED} ${index + 1}\n\nDear Ana, the apples are early this year.\n`)].join("\n"));
    const phases = createPhases(ctx);
    for (const state of states.filter((candidate) => selected.includes(candidate.id))) {
      log(`state ${state.id} (${state.source})`);
      await phases[state.phase]();
      const result = await runState(ctx, state, viewports, recorder, { skipPasses: Boolean(options.quick) });
      report.states.push(result);
      writeReport();
      log(`  ${Object.entries(result.viewports).map(([label, value]) => `${label}: ${value.failures} failures, ${value.hovers} hovers, ${value.focusStops} focus stops, ${value.opens} opens`).join("; ")} (${result.seconds}s)`);
    }
    const ownerFiles = fs.readdirSync(backend.dataDir, { recursive: true }).filter((name) => String(name).endsWith(".orpheus-storage-owner.json"));
    report.isolation = { dataDir: backend.dataDir, dbPath: backend.dbPath, dbInside: fs.existsSync(backend.dbPath), ownerFiles };
  } finally {
    report.finishedAt = new Date().toISOString();
    report.summary = summarize(recorder.failures);
    writeReport();
    await cleanupAll();
  }
  console.log(`\n${recorder.failures.length} distinct failures. Report: ${path.join(out, "report.json")}`);
  for (const [rule, count] of Object.entries(report.summary)) console.log(`  ${rule}: ${count}`);
  return recorder.failures.length ? 1 : 0;
}

function summarize(failures) {
  const counts = {};
  for (const failure of failures) counts[failure.rule] = (counts[failure.rule] ?? 0) + 1;
  return counts;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  process.exitCode = await main().catch((error) => {
    console.error(error.stack ?? String(error));
    return 1;
  });
}

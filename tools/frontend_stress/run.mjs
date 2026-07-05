#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

import { startBackend } from "./lib/backendProcess.mjs";
import { resolveFallbackChromiumExecutable } from "./lib/chromiumResolver.mjs";
import { startFrontend } from "./lib/frontendProcess.mjs";
import { formatDuration, nowIso } from "./lib/util.mjs";
import { SCENARIOS } from "./scenarios.mjs";

async function launchChromium({ headless }) {
  try {
    return await chromium.launch({ headless });
  } catch (err) {
    if (!/Executable doesn't exist/.test(String(err.message))) {
      throw err;
    }
    const executablePath = resolveFallbackChromiumExecutable({ headless });
    if (!executablePath) {
      throw err;
    }
    console.warn(`playwright's pinned chromium build is missing; falling back to cached build at ${executablePath}`);
    return chromium.launch({ headless, executablePath });
  }
}

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..", "..");
const desktopDir = path.join(repoRoot, "desktop");
const fixturesDir = path.join(repoRoot, "tests", "fixtures");
const artifactsDir = path.join(__dirname, "artifacts");

function parseArgs(argv) {
  const args = { only: null, headed: false, keepOpen: false, backendUrl: null, token: null, frontendUrl: null, stateDir: null, frontendPort: 5173 };
  for (const raw of argv) {
    const [flag, value] = raw.includes("=") ? raw.split(/=(.*)/s) : [raw, undefined];
    switch (flag) {
      case "--only":
        args.only = value.split(",").map((s) => s.trim()).filter(Boolean);
        break;
      case "--headed":
        args.headed = true;
        break;
      case "--keep-open":
        args.keepOpen = true;
        break;
      case "--backend-url":
        args.backendUrl = value;
        break;
      case "--token":
        args.token = value;
        break;
      case "--frontend-url":
        args.frontendUrl = value;
        break;
      case "--state-dir":
        args.stateDir = value;
        break;
      case "--frontend-port":
        args.frontendPort = Number(value);
        break;
      case "--help":
      case "-h":
        printHelp();
        process.exit(0);
        break;
      default:
        if (flag.startsWith("--")) {
          console.warn(`unrecognized flag: ${flag}`);
        }
    }
  }
  return args;
}

function printHelp() {
  console.log(`
Orpheus frontend stress-test harness

Usage:
  node run.mjs [options]

Self-starting mode (default): spawns an isolated backend + vite dev server.
  --frontend-port=<n>   Port for the self-started vite dev server (default 5173;
                         the backend's CORS allowlist only accepts
                         http://localhost:5173, so this can only safely be
                         changed if you also relax DESKTOP_DEV_ORIGIN)
  --state-dir=<path>    Directory for isolated backend state (default .state/<timestamp>)

Reuse mode: point the harness at services you already started.
  --backend-url=<url>   e.g. http://127.0.0.1:54231
  --token=<token>       Desktop API bearer token
  --frontend-url=<url>  e.g. http://127.0.0.1:5173
  (all three must be provided together to enable reuse mode)

Other options:
  --only=a,b,c       Comma-separated scenario ids or names to run
  --headed           Launch a visible browser window instead of headless
  --keep-open        Leave the browser + spawned processes running after the run
  --help             Show this message
`);
}

function log(scope, message) {
  const ts = new Date().toISOString().split("T")[1].replace("Z", "");
  console.log(`[${ts}] [${scope}] ${message}`);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  fs.mkdirSync(artifactsDir, { recursive: true });

  const reuseMode = Boolean(args.backendUrl && args.token && args.frontendUrl);
  let backend = null;
  let frontend = null;
  let backendBaseUrl;
  let token;
  let frontendUrl;

  const stateDir = args.stateDir
    ? path.resolve(args.stateDir)
    : path.join(__dirname, ".state", new Date().toISOString().replace(/[:.]/g, "-"));

  if (reuseMode) {
    log("harness", `reuse mode: backend=${args.backendUrl} frontend=${args.frontendUrl}`);
    backendBaseUrl = args.backendUrl;
    token = args.token;
    frontendUrl = args.frontendUrl;
  } else {
    log("harness", `self-start mode: state dir = ${stateDir}`);
    fs.mkdirSync(stateDir, { recursive: true });
    backend = await startBackend({ repoRoot, stateDir, log: (msg) => log("backend", msg) });
    backendBaseUrl = backend.baseUrl;
    token = backend.token;

    frontend = await startFrontend({ desktopDir, port: args.frontendPort, log: (msg) => log("frontend", msg) });
    frontendUrl = frontend.baseUrl;
  }

  try {
    const hadFailure = await runWithBrowser({ args, backend, frontendUrl, backendBaseUrl, token, reuseMode, frontend });
    process.exitCode = hadFailure ? 1 : 0;
  } finally {
    if (!args.keepOpen) {
      if (frontend) await frontend.stop().catch(() => {});
      if (backend) await backend.stop().catch(() => {});
    } else {
      log("harness", "--keep-open set: leaving spawned processes running");
    }
  }
}

async function runWithBrowser({ args, backend, frontendUrl, backendBaseUrl, token, reuseMode, frontend }) {
  const browser = await launchChromium({ headless: !args.headed });
  try {
    return await runScenarios({ args, backend, frontendUrl, backendBaseUrl, token, reuseMode, frontend, browser });
  } finally {
    if (!args.keepOpen) {
      await browser.close().catch(() => {});
    }
  }
}

async function runScenarios({ args, backend, frontendUrl, backendBaseUrl, token, reuseMode, frontend, browser }) {
  const context = await browser.newContext();
  const page = await context.newPage();

  const consoleEvents = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      consoleEvents.push({ ts: nowIso(), kind: "console-error", text: msg.text() });
    }
  });
  page.on("pageerror", (err) => {
    consoleEvents.push({ ts: nowIso(), kind: "pageerror", text: String(err?.stack || err) });
  });
  // Chrome's own "Failed to load resource" console errors don't include the
  // URL/status in msg.text(), which makes them useless for diagnosing what
  // actually failed. Track failed HTTP responses separately so we can
  // annotate console errors with the request that likely caused them.
  const failedResponses = [];
  page.on("response", (response) => {
    if (response.status() >= 400) {
      failedResponses.push({
        ts: nowIso(),
        status: response.status(),
        method: response.request().method(),
        url: response.url(),
      });
    }
  });

  const shared = { ttsAvailable: null, bookTitles: [], pastedBookTitle: null, playerBookTitle: null };

  const ctx = {
    page,
    log: () => {},
    fixturesDir,
    artifactsDir,
    backendBaseUrl,
    token,
    frontendUrl,
    backend,
    shared,
    getConsoleErrorCount: () => consoleEvents.length,
  };

  const scenariosToRun = args.only
    ? SCENARIOS.filter((s) => args.only.includes(s.id) || args.only.includes(s.name))
    : SCENARIOS;

  const results = [];
  let hadFailure = false;

  for (const scenario of scenariosToRun) {
    const scenarioLog = (msg) => log(scenario.name, msg);
    ctx.log = scenarioLog;
    scenarioLog("starting");

    const startIdx = consoleEvents.length;
    const failedResponseStartIdx = failedResponses.length;
    const startedAt = Date.now();
    let status = "pass";
    let error = null;
    let screenshot = null;

    try {
      await scenario.run(ctx);
    } catch (err) {
      if (err && err.isSkip) {
        status = "skip";
        error = err.message;
      } else {
        status = "fail";
        error = err && err.stack ? err.stack : String(err);
      }
    }

    const durationMs = Date.now() - startedAt;
    const scenarioConsoleErrors = consoleEvents.slice(startIdx);
    const scenarioFailedResponses = failedResponses.slice(failedResponseStartIdx);

    if (status === "pass" && !scenario.allowConsoleErrors && scenarioConsoleErrors.length > 0) {
      status = "fail";
      const responseLines = scenarioFailedResponses.map((r) => `  [http ${r.status}] ${r.method} ${r.url}`);
      error = [
        `${scenarioConsoleErrors.length} unexpected browser console error(s):`,
        ...scenarioConsoleErrors.map((e) => `  [${e.kind}] ${e.text}`),
        ...(responseLines.length > 0 ? ["failed HTTP responses during this scenario:", ...responseLines] : []),
      ].join("\n");
    }

    if (status === "fail") {
      hadFailure = true;
      try {
        const screenshotPath = path.join(artifactsDir, `${scenario.id}-${scenario.name}.png`);
        await page.screenshot({ path: screenshotPath, fullPage: true });
        screenshot = screenshotPath;
      } catch (shotErr) {
        scenarioLog(`failed to capture screenshot: ${shotErr.message}`);
      }
    }

    scenarioLog(`${status} (${formatDuration(durationMs)})${error ? ` — ${error.split("\n")[0]}` : ""}`);

    results.push({
      id: scenario.id,
      name: scenario.name,
      status,
      durationMs,
      error,
      consoleErrorCount: scenarioConsoleErrors.length,
      screenshot,
    });
  }

  const report = {
    startedAt: nowIso(),
    mode: reuseMode ? "reuse" : "self-start",
    backendBaseUrl,
    frontendUrl,
    scenarios: results,
    summary: {
      pass: results.filter((r) => r.status === "pass").length,
      fail: results.filter((r) => r.status === "fail").length,
      skip: results.filter((r) => r.status === "skip").length,
      totalDurationMs: results.reduce((sum, r) => sum + r.durationMs, 0),
    },
  };

  const reportPath = path.join(artifactsDir, "report.json");
  fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));

  console.log("\n=== Orpheus frontend stress-test summary ===\n");
  console.table(
    results.map((r) => ({
      id: r.id,
      scenario: r.name,
      status: r.status,
      duration: formatDuration(r.durationMs),
      consoleErrors: r.consoleErrorCount,
      note: r.error ? r.error.split("\n")[0].slice(0, 90) : "",
    })),
  );
  console.log(
    `pass=${report.summary.pass} fail=${report.summary.fail} skip=${report.summary.skip} total=${formatDuration(
      report.summary.totalDurationMs,
    )}`,
  );
  console.log(`\nfull report: ${reportPath}`);
  console.log(`artifacts dir: ${artifactsDir}\n`);

  return hadFailure;
}

main().catch((err) => {
  console.error("harness crashed:", err);
  process.exitCode = 1;
});

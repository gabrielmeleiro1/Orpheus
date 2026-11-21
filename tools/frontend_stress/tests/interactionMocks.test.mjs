// Every payload the interaction audit mocks must have the backend's wire
// shape: the desktop app's wire fixtures where one exists, otherwise the
// response interface in desktop/src/types.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  downloadFailed, downloadRunning, failedActive, failedEventStream, libraryCount, libraryPage, runningActive,
  isGuardedRequest, mockedSuccess, runtimeNotReadyStatus,
} from "../lib/interactionMocks.mjs";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../desktop");
const fixture = (name) => JSON.parse(fs.readFileSync(path.join(desktop, "src/test/wire-fixtures", `${name}.json`), "utf8"));

const kind = (value) => (value === null ? "null" : Array.isArray(value) ? "array" : typeof value);

/** Same keys and value kinds as the fixture, recursively (null matches anything nullable there). */
function assertShape(actual, expected, where = "payload") {
  assert.equal(kind(actual), kind(expected) === "null" ? kind(actual) : kind(expected), `${where}: kind`);
  if (kind(expected) === "object") {
    assert.deepEqual(Object.keys(actual).sort(), Object.keys(expected).sort(), `${where}: keys`);
    for (const key of Object.keys(expected)) {
      if (expected[key] !== null && actual[key] !== null) assertShape(actual[key], expected[key], `${where}.${key}`);
    }
  } else if (kind(expected) === "array" && expected.length && actual.length) {
    assertShape(actual[0], expected[0], `${where}[0]`);
  }
}

/** Required and optional fields of an exported interface in desktop/src/types. */
function interfaceFields(name) {
  for (const file of fs.readdirSync(path.join(desktop, "src/types"))) {
    const source = fs.readFileSync(path.join(desktop, "src/types", file), "utf8");
    const start = new RegExp(`interface ${name}(?: extends [^{]+)? \\{`).exec(source);
    if (!start) continue;
    // Top-level members only: nested object types have fields of their own.
    let depth = 1;
    let body = "";
    for (let index = start.index + start[0].length; index < source.length && depth > 0; index += 1) {
      const char = source[index];
      if (char === "{") depth += 1;
      if (char === "}") depth -= 1;
      if (depth === 1 && char !== "}") body += char;
      else if (depth > 1) body += char === "\n" ? "\n" : " ";
    }
    const fields = [...body.matchAll(/^\s*([a-z_]+)(\?)?:/gm)];
    return { required: fields.filter((field) => !field[2]).map((field) => field[1]), optional: fields.filter((field) => field[2]).map((field) => field[1]) };
  }
  throw new Error(`interface ${name} not found`);
}

function assertInterface(payload, name, { extraAllowed = [] } = {}) {
  const { required, optional } = interfaceFields(name);
  for (const field of required) assert.ok(field in payload, `${name}: missing ${field}`);
  for (const field of Object.keys(payload)) assert.ok([...required, ...optional, ...extraAllowed].includes(field), `${name}: unknown field ${field}`);
}

test("download mocks match the model-download wire fixture", () => {
  assertShape(downloadRunning(), fixture("model-download-running"));
  assertShape(downloadFailed(), fixture("model-download-running"));
  assert.equal(downloadFailed().lifecycle, "failed");
  assert.match(downloadFailed().error, /huggingface\.co/, "a download error, not a conversion error");
});

test("the not-ready runtime is a state the backend emits", () => {
  const status = runtimeNotReadyStatus(fixture("setup-status-configured"));
  assertShape(status, fixture("setup-status-configured"));
  const states = /type RuntimeSetupState = ([^;]+);/.exec(fs.readFileSync(path.join(desktop, "src/types/api.ts"), "utf8"))[1];
  assert.ok(states.includes(`"${status.state}"`), `${status.state} is not a RuntimeSetupState`);
  assert.equal(status.ready, false);
});

test("library pages answer the request's own limit and offset", () => {
  const real = fixture("library-list");
  const item = (index) => ({ ...real.items[0], id: 100_000 + index, title: `Book ${index}` });
  const url = new URL("http://backend/api/library?limit=100&offset=100");
  const page = libraryPage(real, url, 200, item);
  assertShape(page, real);
  assert.equal(page.items.length, 100);
  assert.equal(page.items[0].title, "Book 100");
  assert.equal(libraryPage(real, new URL("http://backend/api/library?limit=100&offset=200"), 200, item).items.length, 0);
  assert.deepEqual(Object.keys(libraryCount(200)), ["count"]);
});

test("the failed job and its events match the active and SSE fixtures", () => {
  const active = failedActive({ title: "Book", author: null, error: "Chapter 7 failed" });
  assertShape(active, fixture("active-completed"));
  assertShape(runningActive({ title: "Book", author: null }), fixture("active-running"));
  const stream = failedEventStream({ backendInstanceId: "backend-1", error: "Chapter 7 failed" });
  const events = [...stream.matchAll(/^data: (.+)$/gm)].map(([, json]) => JSON.parse(json));
  assert.ok(events.length >= 3);
  for (const event of events) assertShape(event, fixture(event.event_type === "state" ? "sse-state" : "sse-log"), event.event_type);
  assert.equal(events.at(-1).state, "failed");
});

test("mocked successes for blocked requests have their response shapes", () => {
  assertShape(mockedSuccess("POST", "/api/conversions", { client_request_id: "desktop-1" }), fixture("conversion-start"));
  assertInterface(mockedSuccess("DELETE", "/api/library/7"), "DeleteBookResponse", { extraAllowed: ["library_id", "library_epoch"] });
  assertInterface(mockedSuccess("POST", "/api/library/7/open-folder"), "OpenBookFolderResponse");
  assertInterface(mockedSuccess("POST", "/api/maintenance/backups/b1/restore"), "MaintenanceRestoreResponse");
  assertInterface(mockedSuccess("POST", "/api/maintenance/backup"), "MaintenanceBackupResponse");
  assertInterface(mockedSuccess("DELETE", "/api/setup/models/cleaner-cuda"), "RuntimeSetupRuntimeDeleteResponse");
  assertInterface(mockedSuccess("DELETE", "/api/setup/runtime"), "RuntimeSetupRuntimeDeleteResponse");
  assertInterface(mockedSuccess("POST", "/api/conversions/job-1/cancel"), "CancelConversionResponse");
  assertShape(mockedSuccess("POST", "/api/setup/models/download"), fixture("model-download-running"));
  assert.deepEqual(Object.keys(mockedSuccess("POST", "/api/system/storage/output/open")), ["folder_path"]);
  assert.equal(mockedSuccess("GET", "/api/library"), null);
});

test("every guarded request has a mocked success, and reads pass through", () => {
  for (const [method, pathname] of [
    ["POST", "/api/maintenance/backup"], ["POST", "/api/maintenance/cleanup"], ["POST", "/api/conversions"],
    ["DELETE", "/api/library/7"], ["POST", "/api/library/7/open-folder"], ["POST", "/api/setup/models/download"],
  ]) {
    assert.equal(isGuardedRequest(method, pathname), true, `${method} ${pathname}`);
  }
  assert.ok(mockedSuccess("POST", "/api/maintenance/backup"));
  for (const [method, pathname] of [["GET", "/api/maintenance/backups"], ["POST", "/api/sources/preflight"], ["OPTIONS", "/api/library/7"]]) {
    assert.equal(isGuardedRequest(method, pathname), false, `${method} ${pathname}`);
  }
});

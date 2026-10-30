import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { resolveFallbackChromiumExecutable } from "../lib/chromiumResolver.mjs";

function tempHome(t) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "orpheus-chromium-"));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  return home;
}

function touch(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "");
}

test("selects the newest cached headless build for each platform layout", (t) => {
  const cases = [
    ["darwin", (home) => path.join(home, "Library", "Caches", "ms-playwright"), ["chrome-headless-shell-mac-arm64", "chrome-headless-shell"]],
    ["linux", (home) => path.join(home, ".cache", "ms-playwright"), ["chrome-headless-shell-linux64", "chrome-headless-shell"]],
    ["win32", (home) => path.join(home, "Local", "ms-playwright"), ["chrome-headless-shell-win64", "chrome-headless-shell.exe"]],
  ];
  for (const [platform, cacheDir, binary] of cases) {
    const home = tempHome(t);
    const cache = cacheDir(home);
    touch(path.join(cache, "chromium_headless_shell-1217", ...binary));
    touch(path.join(cache, "chromium_headless_shell-1223", ...binary));
    fs.mkdirSync(path.join(cache, "chromium_headless_shell-1300"), { recursive: true });
    const env = { LOCALAPPDATA: path.join(home, "Local") };
    assert.equal(
      resolveFallbackChromiumExecutable({ headless: true, env, platform, home }),
      path.join(cache, "chromium_headless_shell-1223", ...binary),
      platform,
    );
  }
});

test("returns null without a cache or on an unknown platform", (t) => {
  const home = tempHome(t);
  assert.equal(resolveFallbackChromiumExecutable({ headless: true, env: {}, platform: "linux", home }), null);
  assert.equal(resolveFallbackChromiumExecutable({ headless: true, env: {}, platform: "aix", home }), null);
});

test("prefers an explicit executable override and rejects a missing one", (t) => {
  const home = tempHome(t);
  const browser = path.join(home, "chrome.exe");
  touch(browser);
  assert.equal(
    resolveFallbackChromiumExecutable({ headless: true, env: { ORPHEUS_HARNESS_CHROMIUM: browser }, platform: "linux", home }),
    browser,
  );
  assert.throws(
    () => resolveFallbackChromiumExecutable({ headless: true, env: { ORPHEUS_HARNESS_CHROMIUM: path.join(home, "missing") }, platform: "linux", home }),
    /does not exist/,
  );
});

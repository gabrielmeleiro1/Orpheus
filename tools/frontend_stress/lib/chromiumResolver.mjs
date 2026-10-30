import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const CACHE_LAYOUTS = {
  darwin: {
    cacheDir: (home) => path.join(home, "Library", "Caches", "ms-playwright"),
    headless: ["chrome-headless-shell-mac-arm64", "chrome-headless-shell"],
    headed: ["chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"],
  },
  win32: {
    cacheDir: (home, env) => path.join(env.LOCALAPPDATA || path.join(home, "AppData", "Local"), "ms-playwright"),
    headless: ["chrome-headless-shell-win64", "chrome-headless-shell.exe"],
    headed: ["chrome-win64", "chrome.exe"],
  },
  linux: {
    cacheDir: (home) => path.join(home, ".cache", "ms-playwright"),
    headless: ["chrome-headless-shell-linux64", "chrome-headless-shell"],
    headed: ["chrome-linux64", "chrome"],
  },
};

/**
 * The version of `playwright` pinned in package.json can be newer than the
 * browser revision actually cached in the local ms-playwright directory (e.g.
 * playwright-core wants chromium revision 1228 but only 1217/1223 are on
 * disk). Rather than requiring a network-dependent `npx playwright install`,
 * fall back to the newest compatible cached build.
 *
 * `ORPHEUS_HARNESS_CHROMIUM` names an explicit Chromium-based executable
 * (for example an installed Chrome) and takes precedence over the cache.
 *
 * Returns an `executablePath` to pass to `chromium.launch()`, or `null` to
 * let Playwright use its own default resolution (when that already works).
 */
export function resolveFallbackChromiumExecutable({
  headless,
  env = process.env,
  platform = process.platform,
  home = os.homedir(),
}) {
  const override = env.ORPHEUS_HARNESS_CHROMIUM?.trim();
  if (override) {
    if (!fs.existsSync(override)) {
      throw new Error(`ORPHEUS_HARNESS_CHROMIUM does not exist: ${override}`);
    }
    return override;
  }

  const layout = CACHE_LAYOUTS[platform];
  if (!layout) {
    return null;
  }
  const cacheDir = layout.cacheDir(home, env);
  if (!fs.existsSync(cacheDir)) {
    return null;
  }

  const prefix = headless ? "chromium_headless_shell-" : "chromium-";
  const entries = fs
    .readdirSync(cacheDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name.startsWith(prefix))
    .map((entry) => ({ name: entry.name, revision: Number(entry.name.slice(prefix.length)) || 0 }))
    .filter((entry) => Number.isFinite(entry.revision))
    .sort((a, b) => b.revision - a.revision);

  for (const entry of entries) {
    const candidate = path.join(cacheDir, entry.name, ...(headless ? layout.headless : layout.headed));
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  return null;
}

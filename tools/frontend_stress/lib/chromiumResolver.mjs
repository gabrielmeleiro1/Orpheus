import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * The version of `playwright` pinned in package.json can be newer than the
 * browser revision actually cached under ~/Library/Caches/ms-playwright on
 * this machine (e.g. playwright-core wants chromium revision 1228 but only
 * 1217/1223 are on disk). Rather than requiring a network-dependent
 * `npx playwright install`, fall back to the newest compatible cached build.
 *
 * Returns an `executablePath` to pass to `chromium.launch()`, or `null` to
 * let Playwright use its own default resolution (when that already works).
 */
export function resolveFallbackChromiumExecutable({ headless }) {
  const cacheDir = path.join(os.homedir(), "Library", "Caches", "ms-playwright");
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
    const candidate = headless
      ? path.join(cacheDir, entry.name, "chrome-headless-shell-mac-arm64", "chrome-headless-shell")
      : path.join(cacheDir, entry.name, "chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing");
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }

  return null;
}

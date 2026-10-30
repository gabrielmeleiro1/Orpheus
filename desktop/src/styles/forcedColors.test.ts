/// <reference types="node" />
// @vitest-environment node

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Windows High Contrast (forced colors). Chromium paints a Canvas backplate
 * behind text that it adjusts, so a control filled with the accent
 * (Highlight) and labelled in --on-accent (HighlightText) shows its label as
 * a solid block unless it opts out with `forced-color-adjust: none` and paints
 * a system colour pair itself. The interaction audit checks the rendered
 * result; this test keeps the stylesheet rule in place.
 */
const srcDir = join(dirname(fileURLToPath(import.meta.url)), "..");

function cssFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return cssFiles(path);
    return entry.name.endsWith(".css") ? [path] : [];
  });
}

interface Rule { selector: string; body: string; forced: boolean }

function rules(css: string): Rule[] {
  const found: Rule[] = [];
  const forcedBlocks = [...css.matchAll(/@media\s*\(forced-colors:\s*active\)\s*\{((?:[^{}]*\{[^}]*\})*)\s*\}/g)];
  for (const block of forcedBlocks) {
    for (const [, selector, body] of block[1].matchAll(/([^{};]+)\{([^{}]*)\}/g)) found.push({ selector: selector.trim(), body, forced: true });
  }
  const rest = forcedBlocks.reduce((text, block) => text.replace(block[0], ""), css);
  for (const [, selector, body] of rest.matchAll(/([^{};]+)\{([^{}]*)\}/g)) found.push({ selector: selector.trim(), body, forced: false });
  return found;
}

const all = cssFiles(srcDir).flatMap((file) => rules(readFileSync(file, "utf8")));
const strip = (selector: string) => selector.replace(/\/\*[\s\S]*?\*\//g, "").trim();

describe("forced colors", () => {
  it("paints every accent-filled, accent-labelled control with a system colour pair", () => {
    const filled = all.filter((rule) => !rule.forced
      && /background:\s*var\(--accent\)/.test(rule.body) && /color:\s*var\(--on-accent\)/.test(rule.body))
      .map((rule) => strip(rule.selector));
    expect(filled.length).toBeGreaterThan(0);
    for (const selector of filled) {
      if (selector === ".brand-mark") continue; // an icon, no text to backplate
      const forced = all.find((rule) => rule.forced && strip(rule.selector).split(/\s*,\s*/).includes(selector));
      expect(forced?.body, selector).toMatch(/forced-color-adjust:\s*none/);
      expect(forced?.body, selector).toMatch(/background:\s*Highlight/);
      expect(forced?.body, selector).toMatch(/color:\s*HighlightText/);
    }
  });

  it("draws a visible marker on the active sidebar tab", () => {
    const marker = all.find((rule) => rule.forced && strip(rule.selector) === ".rail-tab.active::before");
    expect(marker?.body).toMatch(/content:\s*""/);
    expect(marker?.body).toMatch(/background:\s*Highlight/);
    expect(marker?.body).toMatch(/width:\s*\d/);
  });

  it("pads the end of a flush text button, which gains a border", () => {
    const flush = all.filter((rule) => strip(rule.selector) === ".btn-flush-end");
    // At rest the label meets the column edge: no end padding.
    expect(flush.find((rule) => !rule.forced)?.body).toMatch(/padding-inline:\s*var\(--space-\d\)\s+0\s*;/);
    expect(flush.find((rule) => rule.forced)?.body).toMatch(/padding-inline:\s*var\(--space-\d\)\s*;/);
  });
});

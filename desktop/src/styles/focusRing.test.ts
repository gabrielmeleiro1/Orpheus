/// <reference types="node" />
// @vitest-environment node

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Focus outlines are drawn inside the focused box. An outline outside it is
 * cut off wherever a control touches the edge of a scrolling or clipped pane
 * (the conversion columns, the library and settings panes, the sidebar list).
 */
const srcDir = join(dirname(fileURLToPath(import.meta.url)), "..");

function cssFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return cssFiles(path);
    return entry.name.endsWith(".css") ? [path] : [];
  });
}

/** Rules whose selector shows keyboard focus, outside forced-colors blocks. */
function focusRules(css: string): Array<{ selector: string; body: string }> {
  const withoutForcedColors = css.replace(/@media\s*\(forced-colors:\s*active\)\s*\{(?:[^{}]*\{[^}]*\})*\s*\}/g, "");
  return [...withoutForcedColors.matchAll(/([^{};]+)\{([^{}]*)\}/g)]
    .map(([, selector, body]) => ({ selector: selector.trim(), body }))
    .filter(({ selector }) => /:focus(-visible|-within)?\b/.test(selector));
}

const base = readFileSync(join(srcDir, "styles", "base.css"), "utf8");
const controls = readFileSync(join(srcDir, "styles", "controls.css"), "utf8");

describe("focus ring contract", () => {
  it("defines an inset ring offset as a token", () => {
    expect(/--focus-ring-offset:\s*([^;]+);/.exec(base)?.[1].trim()).toMatch(/^(-|calc\(-1 \* )/);
    // The box-shadow form, used by rows such as the sidebar books, is inset too.
    expect(base).toMatch(/--focus-ring:\s*inset 0 0 0 var\(--focus-ring-width\) var\(--focus-color\)/);
  });

  it("draws the shared ring with the tokens", () => {
    const shared = focusRules(controls).find(({ selector }) => selector.startsWith(":where(button"));
    expect(shared?.body).toMatch(/outline:\s*var\(--focus-ring-width\) solid var\(--focus-color\)/);
    expect(shared?.body).toMatch(/outline-offset:\s*var\(--focus-ring-offset\)/);
  });

  it("never pushes a focus outline outside the focused box", () => {
    const outward: string[] = [];
    for (const file of cssFiles(srcDir)) {
      for (const { selector, body } of focusRules(readFileSync(file, "utf8"))) {
        const offset = /outline-offset:\s*([^;]+)/.exec(body)?.[1].trim();
        const inward = offset === "var(--focus-ring-offset)" || offset === "0" || /^(-|calc\(-1 \* var\(--focus-ring-width\))/.test(offset ?? "");
        if (offset && !inward) {
          outward.push(`${file}: ${selector} { outline-offset: ${offset} }`);
        }
      }
    }
    expect(outward).toEqual([]);
  });
});

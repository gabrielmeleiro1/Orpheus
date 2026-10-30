/// <reference types="node" />
// @vitest-environment node

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Settings model rows use fixed side columns so rows align. The status column
 * must fit its longest state, the four-cell ASCII loader plus "Downloading…",
 * which Chromium draws 128px wide at the 12px status size (measured with the
 * interaction audit's model-download-running state). At 112px the text ran
 * 16px into the Delete column.
 */
const LONGEST_STATUS_PX = 128;

const setupCss = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "setup.css"), "utf8");

const baseCss = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "../../styles/base.css"), "utf8");
const token = (name: string) => Number.parseFloat(new RegExp(`--${name}:\\s*([\\d.]+)px`).exec(baseCss)?.[1] ?? "NaN");

/** Column widths at the default text size; each must scale with a text token. */
function columns(selector: string): number[] {
  const rule = new RegExp(`${selector.replace(/[.]/g, "\\.")}[^{]*\\{([^}]*)\\}`).exec(setupCss)?.[1] ?? "";
  const template = /grid-template-columns:\s*([^;]+);/.exec(rule)?.[1] ?? "";
  return [...template.matchAll(/calc\(var\(--(text-[a-z]+)\) \* ([\d.]+)\)/g)].map(([, name, factor]) => token(name) * Number(factor));
}

describe("settings model row columns", () => {
  it("gives the status column room for the downloading state", () => {
    // Three text-sized side columns; px widths would not grow with enlarged text.
    const [size, status, action] = columns(".setup-managed-model-main");
    expect([size, status, action].every(Number.isFinite)).toBe(true);
    expect(status).toBeGreaterThanOrEqual(LONGEST_STATUS_PX);
    expect(size).toBeGreaterThanOrEqual(60);
    expect(action).toBeGreaterThanOrEqual(96);
  });
});

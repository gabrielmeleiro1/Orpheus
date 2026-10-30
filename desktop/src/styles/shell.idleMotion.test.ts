/// <reference types="node" />
// @vitest-environment node

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * Settled-dashboard idle CPU is dominated by the main/WebKit process. An
 * infinite CSS animation on the always-visible connected status dot keeps the
 * compositor scheduling frames even when every canvas RAF loop is stopped.
 */
const shellCss = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "shell.css"), "utf8");

describe("shell idle motion contract", () => {
  it("does not infinitely animate the connected status dot", () => {
    expect(shellCss).toMatch(/\.status-dot\.is-connected/);
    expect(shellCss).not.toMatch(/\.status-dot\.is-connected[^{]*\{[^}]*animation\s*:/s);
  });
});

/// <reference types="node" />
// @vitest-environment node

import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const srcDir = join(dirname(fileURLToPath(import.meta.url)), "..");

function files(dir: string, extension: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return files(path, extension);
    return entry.name.endsWith(extension) ? [path] : [];
  });
}

const base = readFileSync(join(srcDir, "styles", "base.css"), "utf8");
const controls = readFileSync(join(srcDir, "styles", "controls.css"), "utf8");
const rootBlock = base.slice(base.indexOf(":root {"), base.indexOf("}", base.indexOf(":root {")));
const tokens = new Map([...rootBlock.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value.trim()]));

function resolve(value: string): string {
  return value.replace(/var\((--[\w-]+)\)/g, (_, name: string) => resolve(tokens.get(name) ?? ""));
}

function rgb(value: string): [number, number, number] {
  const hex = resolve(value).match(/^#([0-9a-f]{6})$/i);
  if (!hex) throw new Error(`not an opaque hex colour: ${value}`);
  return [0, 2, 4].map((offset) => parseInt(hex[1].slice(offset, offset + 2), 16)) as [number, number, number];
}

function contrast(foreground: string, background: string): number {
  const luminance = (colour: [number, number, number]) => {
    const [r, g, b] = colour.map((channel) => {
      const value = channel / 255;
      return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const [light, dark] = [luminance(rgb(foreground)), luminance(rgb(background))].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
}

describe("design tokens", () => {
  it("keeps every text token at WCAG AA (4.5:1) on every surface it is drawn on", () => {
    const surfaces = ["var(--frame)", "var(--surface-0)", "var(--surface-1)", "var(--surface-2)", "var(--surface-3)"];
    for (const text of ["var(--text)", "var(--text-secondary)", "var(--text-tertiary)"]) {
      for (const surface of surfaces) {
        expect(contrast(text, surface), `${text} on ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    }
    // Pressed and selected fills carry primary and secondary text only.
    for (const text of ["var(--text)", "var(--text-secondary)"]) {
      expect(contrast(text, "var(--surface-active)"), `${text} on the active fill`).toBeGreaterThanOrEqual(4.5);
    }
    expect(contrast("var(--on-accent)", "var(--accent)")).toBeGreaterThanOrEqual(4.5);
  });

  it("draws a disabled selected segment on a fill its tertiary text stays legible on", () => {
    const rule = controls.match(/:is\(\.seg-control-btn:disabled, :disabled \.seg-control-btn\)\.is-active\s*\{([^}]*)\}/);
    expect(rule, "disabled selected segment rule").not.toBeNull();
    const fill = rule![1].match(/background:\s*([^;]+);/)![1].trim();
    expect(contrast("var(--text-tertiary)", fill)).toBeGreaterThanOrEqual(4.5);
  });

  it("defines every custom property the stylesheets read", () => {
    const css = files(srcDir, ".css").map((path) => readFileSync(path, "utf8")).join("\n");
    const tsx = files(srcDir, ".tsx").filter((path) => !path.endsWith(".test.tsx")).map((path) => readFileSync(path, "utf8")).join("\n");
    const declared = new Set([...css.matchAll(/(--[\w-]+)\s*:/g)].map(([, name]) => name));
    const setInline = new Set([...tsx.matchAll(/"(--[\w-]+)"/g)].map(([, name]) => name));
    const read = new Set([...css.matchAll(/var\((--[\w-]+)/g)].map(([, name]) => name));
    const missing = [...read].filter((name) => !declared.has(name) && !setInline.has(name));
    expect(missing).toEqual([]);
  });
});

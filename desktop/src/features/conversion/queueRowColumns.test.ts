/// <reference types="node" />
// @vitest-environment happy-dom

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToString } from "react-dom/server";

import { describe, expect, it } from "vitest";

import { conversionPanelViewModel } from "../../test/conversionPanelViewModel";
import { SourceQueueView } from "./views/SourceQueueView";

/**
 * The queue row's tools (up, down, remove) are positioned over the row's last
 * column. A "Ready" row draws nothing there, so unless that column is as wide
 * as the tools, a long title runs underneath them and its end cannot be read.
 */
const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "conversion.css"), "utf8");
const baseCss = readFileSync(join(here, "../../styles/base.css"), "utf8");
const token = (name: string) => Number.parseFloat(new RegExp(`--${name}:\\s*([\\d.]+)px`).exec(baseCss)?.[1] ?? "NaN");

function declaration(selector: string, property: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const body = new RegExp(`(?:^|\\}|\\*/)\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css)?.[1] ?? "";
  return new RegExp(`${property}:\\s*([^;]+);`).exec(body)?.[1].trim() ?? "";
}

function renderedRowTools(): number {
  const model = conversionPanelViewModel();
  model.sourceQueue.queueCount = 1;
  model.sourceQueue.queueItems = [{
    id: "book", title: "A long title", selected: false, statusText: "Ready",
    statusClassName: "queue-item-status status-ready", converting: false,
    canMoveUp: true, canMoveDown: true, canRemove: true,
  }];
  const host = document.createElement("div");
  host.innerHTML = renderToString(createElement(SourceQueueView, {
    sourceQueue: model.sourceQueue, capabilities: model.capabilities, actions: model.actions,
  }));
  return host.querySelectorAll(".queue-row .queue-item-actions button").length;
}

describe("queue row columns", () => {
  it("ends the title before the row tools", () => {
    const columns = declaration(".queue-row:not(.is-converting) > .queue-item-main", "grid-template-columns");
    const reserved = /minmax\(calc\((\d+) \* var\(--control-h-sm\) - \(var\(--space-3\) - var\(--space-2\)\)\), auto\)$/.exec(columns);
    expect(reserved).not.toBeNull();
    // The title column may shrink; the last one may not.
    expect(columns).toContain("minmax(0, 1fr)");

    // Every rendered icon button, anchored `right` inside the row's own padding.
    const toolCount = renderedRowTools();
    expect(toolCount).toBeGreaterThan(0);
    const tools = toolCount * token("control-h-sm");
    expect(declaration(".queue-item-actions", "right")).toBe("var(--space-2)");
    expect(declaration(".queue-row", "padding")).toBe("var(--space-2) var(--space-3)");
    const toolsLeftEdge = token("space-2") + tools;
    const titleRightLimit = token("space-3") + Number(reserved?.[1]) * token("control-h-sm") - (token("space-3") - token("space-2"));
    expect(titleRightLimit).toBeGreaterThanOrEqual(toolsLeftEdge);
  });
});

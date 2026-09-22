// @vitest-environment happy-dom

import { renderToString } from "react-dom/server";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import { LogPanel } from "./LogPanel";

describe("LogPanel", () => {
  it("lets wrapped rows grow and clears the buffer while accepting new entries", async () => {
    const logs = [
      { id: 2, level: "info", ts: "12:00", msg: "[PERF] worker_pool_reuse_hits=1 ".repeat(50) },
      { id: 1, level: "info", ts: "12:00", msg: "state completed" },
    ];
    const container = document.createElement("div");
    document.body.append(container);
    const style = document.createElement("style");
    style.textContent = readFileSync("src/features/logs/logs.css", "utf8");
    document.head.append(style);
    const root = createRoot(container);
    try {
      await act(async () => root.render(<LogPanel logs={logs} />));
      expect(getComputedStyle(container.querySelector(".log-line")!).flexShrink).toBe("0");
      expect(container.querySelector(".log-msg")?.textContent).toBe(logs[0].msg);
      await act(async () => [...container.querySelectorAll("button")].find(button => button.textContent === "Clear")!.click());
      expect(container.querySelectorAll(".log-line")).toHaveLength(0);
      expect(container.textContent).toContain("0 entries");
      await act(async () => root.render(<LogPanel logs={[{ ...logs[0], id: 3, msg: "new event" }, ...logs]} />));
      expect(container.querySelectorAll(".log-line")).toHaveLength(1);
      expect(container.querySelector(".log-msg")?.textContent).toBe("new event");
    } finally {
      await act(async () => root.unmount());
      container.remove();
      style.remove();
    }
  });
  it("uses the same sizing contract for all three primary filters", () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LogPanel logs={[]} />);

    expect(container.querySelector(".log-level-control")?.classList.contains("log-filter-control")).toBe(true);
    expect(container.querySelector("#log-category-filter")?.classList.contains("log-filter-control")).toBe(true);
    expect(container.querySelector("#log-search")?.classList.contains("log-filter-control")).toBe(true);
  });
});

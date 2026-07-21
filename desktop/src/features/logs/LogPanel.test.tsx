// @vitest-environment happy-dom

import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { LogPanel } from "./LogPanel";

describe("LogPanel", () => {
  it("uses the same sizing contract for all three primary filters", () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(<LogPanel logs={[]} />);

    expect(container.querySelector(".log-level-control")?.classList.contains("log-filter-control")).toBe(true);
    expect(container.querySelector("#log-category-filter")?.classList.contains("log-filter-control")).toBe(true);
    expect(container.querySelector("#log-search")?.classList.contains("log-filter-control")).toBe(true);
  });
});

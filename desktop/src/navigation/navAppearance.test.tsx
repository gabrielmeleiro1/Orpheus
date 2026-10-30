// @vitest-environment happy-dom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { readFileSync } from "node:fs";
import { expect, it } from "vitest";

it("reserves the selected background for the active tab after navigation", async () => {
  const container = document.createElement("div");
  document.body.append(container);
  const style = document.createElement("style");
  style.textContent = readFileSync("src/styles/shell.css", "utf8");
  document.head.append(style);
  const root = createRoot(container);
  const renderTabs = (active: string) => root.render(<nav>{["Create", "Library"].map(label => <button key={label} role="tab" aria-selected={active === label} className={`rail-tab${active === label ? " active" : ""}`}>{label}</button>)}</nav>);
  try {
    await act(async () => renderTabs("Create"));
    const previous = container.querySelector<HTMLButtonElement>("button")!;
    previous.focus();
    await act(async () => renderTabs("Library"));
    expect(getComputedStyle(previous).backgroundColor).toBe("transparent");
    const rules = [...style.sheet!.cssRules] as CSSStyleRule[];
    // The ring must also win over the active tab's own inset border: the active
    // tab is the tablist's only Tab stop.
    const ringRule = rules.find(rule => rule.selectorText?.split(/\s*,\s*/).includes(".rail-tab:focus-visible"));
    expect(ringRule?.style.boxShadow).toBe("var(--focus-ring)");
    expect(ringRule?.selectorText.split(/\s*,\s*/)).toContain(".rail-tab.active:focus-visible");
    expect(rules.some(rule => rule.selectorText === ".rail-tab:focus")).toBe(false);
    expect(rules.find(rule => rule.selectorText?.includes(".rail-tab:hover"))?.style.background).toBe("");
  } finally {
    await act(async () => root.unmount());
    style.remove();
    container.remove();
  }
});

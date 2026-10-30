// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installFocusRescue } from "./focusRescue";

let uninstall: () => void;
const frames = () => new Promise((resolve) => setTimeout(resolve, 60));

function box<T extends HTMLElement>(element: T, left: number, top: number, width = 80, height = 30): T {
  element.getBoundingClientRect = () => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) });
  return element;
}

function button(label: string, left: number, top: number) {
  const element = document.createElement("button");
  element.textContent = label;
  return box(element, left, top);
}

/** happy-dom keeps a removed or disabled element as activeElement; a browser drops focus to <body>. */
function dropFocus(element: HTMLElement) {
  element.blur();
}

beforeEach(() => {
  document.body.replaceChildren();
  vi.spyOn(document, "hasFocus").mockReturnValue(true);
  uninstall = installFocusRescue(document);
});

afterEach(() => {
  uninstall();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});

describe("focus rescue", () => {
  it("moves focus to the nearest surviving control when the focused one is removed", async () => {
    const list = document.createElement("section");
    const first = button("Remove first", 10, 10);
    const second = button("Remove second", 10, 50);
    const far = button("Start", 600, 400);
    list.append(first, second, far);
    document.body.append(list);
    second.focus();
    dropFocus(second);
    second.remove();
    await frames();
    expect(document.activeElement).toBe(first);
  });

  it("waits for a busy control to come back instead of moving on", async () => {
    const panel = document.createElement("section");
    const save = button("Back up", 10, 10);
    const other = button("Clean up", 10, 50);
    panel.append(save, other);
    document.body.append(panel);
    save.focus();
    dropFocus(save);
    save.disabled = true;
    await frames();
    expect(document.activeElement).toBe(document.body);
    save.disabled = false;
    await frames();
    expect(document.activeElement).toBe(save);
  });

  it("does not leave focus on the page for long while waiting for a busy control", async () => {
    // Add to queue and a saved Save stay disabled for good; a keyboard user
    // must not sit on <body> for a noticeable time before focus moves on.
    const panel = document.createElement("section");
    const save = button("Save", 10, 10);
    const cancel = button("Cancel", 100, 10);
    panel.append(save, cancel);
    document.body.append(panel);
    save.focus();
    dropFocus(save);
    save.disabled = true;
    await new Promise((resolve) => setTimeout(resolve, 900));
    expect(document.activeElement).toBe(cancel);
  });

  it("leaves focus alone when the user clicked away", async () => {
    const panel = document.createElement("section");
    const item = button("Item", 10, 10);
    const other = button("Other", 10, 50);
    panel.append(item, other);
    document.body.append(panel);
    item.focus();
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    dropFocus(item);
    await frames();
    expect(document.activeElement).toBe(document.body);
  });

  it("keeps focus inside an open modal", async () => {
    const outside = button("Outside", 0, 0);
    const dialog = document.createElement("aside");
    dialog.setAttribute("aria-modal", "true");
    const cancel = button("Cancel conversion", 300, 300);
    const close = button("Close", 600, 20);
    dialog.append(cancel, close);
    document.body.append(outside, dialog);
    cancel.focus();
    dropFocus(cancel);
    cancel.remove();
    await frames();
    expect(document.activeElement).toBe(close);
  });

  it("lands on the marked landing point when the whole view was replaced", async () => {
    const root = document.createElement("div");
    root.id = "root";
    const setup = document.createElement("main");
    const proceed = button("Continue", 400, 500);
    setup.append(proceed);
    const heading = document.createElement("h1");
    heading.tabIndex = -1;
    heading.setAttribute("data-focus-landing", "");
    root.append(setup, heading);
    document.body.append(root);
    proceed.focus();
    dropFocus(proceed);
    setup.remove();
    await frames();
    expect(document.activeElement).toBe(heading);
  });

  it("does not pull focus back after Tab moves past the last control", async () => {
    const only = button("Last", 10, 10);
    document.body.append(only);
    only.focus();
    only.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
    dropFocus(only);
    await frames();
    expect(document.activeElement).toBe(document.body);
  });
});

describe("focus rescue reports", () => {
  it("announces each rescue and why, so a dropped focus is still visible to tests", async () => {
    const kinds: string[] = [];
    const listener = (event: Event) => kinds.push((event as CustomEvent<{ kind: string }>).detail.kind);
    document.addEventListener("focusrescue", listener);
    try {
      const panel = document.createElement("section");
      const keep = button("Still here", 10, 10);
      const gone = button("Removed", 10, 50);
      panel.append(keep, gone);
      document.body.append(panel);
      keep.focus();
      keep.blur(); // focus dropped while the control is still usable: a bug elsewhere
      await frames();
      gone.focus();
      gone.blur();
      gone.remove();
      await frames();
      expect(kinds).toEqual(["refocus", "nearest"]);
    } finally {
      document.removeEventListener("focusrescue", listener);
    }
  });
});

describe("busy controls", () => {
  it("reports a control that disabled itself for a moment as re-enabled, not as dropped focus", async () => {
    const kinds: string[] = [];
    const listener = (event: Event) => kinds.push((event as CustomEvent<{ kind: string }>).detail.kind);
    document.addEventListener("focusrescue", listener);
    try {
      const backup = button("Back up", 10, 10);
      document.body.append(backup);
      backup.focus();
      backup.blur(); // a browser drops focus when the focused button becomes disabled
      backup.disabled = true;
      await new Promise((resolve) => setTimeout(resolve, 5));
      backup.disabled = false;
      await frames();
      expect(document.activeElement).toBe(backup);
      expect(kinds).toEqual(["re-enabled"]);
    } finally {
      document.removeEventListener("focusrescue", listener);
    }
  });
});

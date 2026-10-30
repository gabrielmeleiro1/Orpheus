// @vitest-environment happy-dom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { TooltipLayer, truncatedTextAnchor } from "./TooltipLayer";

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.useFakeTimers();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<TooltipLayer />));
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** A span the layout engine would draw with an ellipsis (happy-dom has no layout). */
function ellipsized(text: string, { cut = true, title }: { cut?: boolean; title?: string } = {}) {
  const span = document.createElement("span");
  span.textContent = text;
  span.style.textOverflow = "ellipsis";
  span.style.overflow = "hidden";
  if (title) span.title = title;
  Object.defineProperty(span, "clientWidth", { configurable: true, value: 120 });
  Object.defineProperty(span, "scrollWidth", { configurable: true, value: cut ? 480 : 120 });
  return span;
}

function rowWith(...children: HTMLElement[]) {
  const button = document.createElement("button");
  button.append(...children);
  document.body.append(button);
  return button;
}

async function hover(element: Element) {
  await act(async () => {
    element.dispatchEvent(new PointerEvent("pointerover", { bubbles: true }));
    vi.advanceTimersByTime(500);
  });
}

const bubble = () => document.querySelector(".tooltip-bubble");

it("shows the full text of an ellipsized title inside a hovered control", async () => {
  const title = ellipsized("A Remarkably Long Account of the Lighthouse Keepers of the Northern Coast");
  const author = ellipsized("Mara Quinn", { cut: false });
  const row = rowWith(title, author);
  expect(truncatedTextAnchor(title)).toEqual({ anchor: row, text: title.textContent });
  await hover(title);
  expect(bubble()?.textContent).toBe(title.textContent);
});

it("joins every cut-short value in the control", async () => {
  const title = ellipsized("ThequickbrownfoxjumpsoverthelazydogThequickbrownfox");
  const author = ellipsized("Professor Emerita Henrietta Wilhelmina Fairweather-Montgomery");
  rowWith(title, author);
  await hover(author);
  expect(bubble()?.textContent).toBe(`${title.textContent} · ${author.textContent}`);
});

it("stays quiet for text that fits or that already has a title", async () => {
  const fits = ellipsized("Short", { cut: false });
  rowWith(fits);
  await hover(fits);
  expect(bubble()).toBeNull();
  const titled = ellipsized("A long value with a native tooltip", { title: "A long value with a native tooltip" });
  rowWith(titled);
  expect(truncatedTextAnchor(titled)).toBeNull();
});

it("checks plain text and its near ancestors, not whole regions", () => {
  const span = ellipsized("C:\\Users\\reader\\Documents\\Audiobooks\\Very long folder name");
  const wrapper = document.createElement("div");
  wrapper.append(span);
  document.body.append(wrapper);
  expect(truncatedTextAnchor(span)).toEqual({ anchor: span, text: span.textContent });
  // Hovering the wrapper itself does not search its descendants.
  expect(truncatedTextAnchor(wrapper)).toBeNull();
});

it("puts the cut-short text in front of an explicit data-tip", async () => {
  const title = ellipsized("Mill Stories: A Remarkably Long Account of the Northern Coast");
  const row = rowWith(title);
  row.setAttribute("data-tip", "Show stages and the event log");
  await hover(title);
  expect(bubble()?.textContent).toBe(`${title.textContent} · Show stages and the event log`);
});

it("keeps a focus tip while focus scrolls its control into view, and drops a hover tip on scroll", async () => {
  const row = rowWith(ellipsized("A Remarkably Long Account of the Lighthouse Keepers"));
  row.setAttribute("data-tip", "Open");
  row.focus();
  await hover(row);
  expect(bubble()).not.toBeNull();
  // happy-dom reports zero-size boxes; give the focused row a box on screen.
  row.getBoundingClientRect = () => ({ top: 10, bottom: 40, left: 10, right: 200, width: 190, height: 30, x: 10, y: 10, toJSON: () => ({}) });
  await act(async () => window.dispatchEvent(new Event("scroll")));
  expect(bubble()).not.toBeNull();
  row.blur();
  const other = rowWith(ellipsized("Another title that is far too long"));
  await hover(other);
  await act(async () => window.dispatchEvent(new Event("scroll")));
  expect(bubble()).toBeNull();
});

it("keeps a hover tip whose text is still under the pointer when a scroll lands after the pointer", async () => {
  // A cut-short value outside any control, as in a preview list.
  const title = ellipsized("東京の古い灯台守と海辺の町で過ごした長い冬の日々の記録");
  const item = document.createElement("li");
  item.append(title);
  document.body.append(item);
  const matches = title.matches.bind(title);
  title.matches = (selector: string) => selector === ":hover" || matches(selector);
  title.getBoundingClientRect = () => ({ top: 10, bottom: 30, left: 10, right: 130, width: 120, height: 20, x: 10, y: 10, toJSON: () => ({}) });
  await act(async () => {
    title.dispatchEvent(new PointerEvent("pointerover", { bubbles: true }));
    window.dispatchEvent(new Event("scroll"));
    vi.advanceTimersByTime(500);
  });
  expect(bubble()?.textContent).toBe(title.textContent);
});

it("hides the tip when its control is removed while the tip is showing", async () => {
  const row = rowWith(ellipsized("Mill Stories: A Remarkably Long Account"));
  row.setAttribute("data-tip", "Show stages and the event log");
  await hover(row);
  expect(bubble()).not.toBeNull();
  // A finished job's rail button unmounts: no pointer or focus event fires.
  await act(async () => row.remove());
  expect(bubble()).toBeNull();
});

it("can be hovered: moving onto the tip keeps it, leaving both hides it", async () => {
  const row = rowWith(ellipsized("A Remarkably Long Account of the Lighthouse Keepers"));
  await hover(row);
  const tip = bubble()!;
  expect(tip).not.toBeNull();
  await act(async () => {
    row.dispatchEvent(new PointerEvent("pointerout", { bubbles: true, relatedTarget: document.body }));
    tip.dispatchEvent(new PointerEvent("pointerover", { bubbles: true, relatedTarget: row }));
    vi.advanceTimersByTime(500);
  });
  expect(bubble()).not.toBeNull();
  await act(async () => {
    tip.dispatchEvent(new PointerEvent("pointerout", { bubbles: true, relatedTarget: document.body }));
    vi.advanceTimersByTime(500);
  });
  expect(bubble()).toBeNull();
});

import test from "node:test";
import assert from "node:assert/strict";

import {
  VIEWPORTS, checkClipping, checkCoverage, checkFloatingInViewport, checkFocusIndicator, checkFocusLanding, checkFocusVisible,
  checkForcedLabel, checkForcedState, checkHorizontalScroll, checkScrollAreaUnderBar, checkTextClipping, checkTextCoverage, checkTooltip, checkTruncation,
  failureKey, hoverTipLingers, layoutShifts, overhang, parseArgs, rebuiltTwins, ringBandArea, selectStates, selectViewports,
} from "../lib/interactionChecks.mjs";

const viewport = { w: 960, h: 640, cw: 960, ch: 640 };
const rules = (failures) => failures.map((failure) => failure.rule);
const clip = (overrides = {}) => ({
  id: 1, desc: "div.scroller", overflowX: "hidden", overflowY: "auto",
  box: { x: 0, y: 0, w: 200, h: 100 }, scrollLeft: 0, scrollTop: 0, maxScrollLeft: 0, maxScrollTop: 0, ...overrides,
});

test("arguments, viewports and states are parsed strictly", () => {
  assert.deepEqual(parseArgs(["--state=a,b", "--headed"]), { state: "a,b", headed: true });
  assert.throws(() => parseArgs(["state=a"]), /Unknown argument/);
  assert.throws(() => parseArgs(["--states=library-*"]), /Unknown argument: --states/);
  assert.throws(() => parseArgs(["--viewports=960"]), /Unknown argument: --viewports/);
  assert.deepEqual(parseArgs(["--out=x", "--viewport=960", "--list-states", "--quick"]), { out: "x", viewport: "960", "list-states": true, quick: true });
  assert.deepEqual(selectViewports(undefined), [...VIEWPORTS]);
  assert.deepEqual(selectViewports("960x640,1920").map((v) => v.width), [960, 1920]);
  // Any explicit size can be audited, to probe the smallest supported window.
  assert.deepEqual(selectViewports("800x600"), [{ width: 800, height: 600 }]);
  assert.throws(() => selectViewports("huge"), /Unknown --viewport/);
  assert.deepEqual(selectStates("library-*,settings", ["settings", "library-a", "library-b", "convert"]), ["settings", "library-a", "library-b"]);
  assert.throws(() => selectStates("nope", ["settings"]), /Unknown --state/);
});

test("the four audited window sizes are fixed", () => {
  assert.deepEqual(VIEWPORTS.map((v) => `${v.width}x${v.height}`), ["960x640", "1200x860", "1440x900", "1920x1080"]);
});

test("overhang measures how far a box sticks out on each side", () => {
  assert.deepEqual(overhang({ x: -5, y: 10, w: 20, h: 100 }, { x: 0, y: 0, w: 100, h: 100 }), { left: 5, top: -10, right: -85, bottom: 10 });
});

test("floating layers must be fully inside the window", () => {
  assert.deepEqual(checkFloatingInViewport({ desc: "tip", rect: { x: 10, y: 10, w: 100, h: 20 } }, viewport), []);
  const [failure] = checkFloatingInViewport({ desc: "menu", rect: { x: 900, y: 600, w: 100, h: 80 } }, viewport);
  assert.equal(failure.rule, "floating-outside-viewport");
  assert.match(failure.detail, /right 40px, bottom 40px/);
});

test("content scrolled out of a scroll container is fine while scrolling can reveal it", () => {
  const element = { desc: "button", rect: { x: 10, y: 90, w: 50, h: 30 } };
  assert.deepEqual(checkClipping({ ...element, chain: [clip({ maxScrollTop: 400 })] }), []);
  // Scrolled to the end: the remaining 20px can never be reached.
  assert.deepEqual(rules(checkClipping({ ...element, chain: [clip({ maxScrollTop: 400, scrollTop: 400 })] })), ["clipped-by-ancestor"]);
  // overflow: hidden is never reachable.
  assert.deepEqual(rules(checkClipping({ ...element, chain: [clip({ overflowY: "hidden" })] })), ["clipped-by-ancestor"]);
});

test("an outer pane only clips what an inner scroll container leaves visible", () => {
  // The row is below an inner list that can still scroll; the clipping pane
  // around that list must not report the row a second time.
  const element = { desc: "row", rect: { x: 10, y: 150, w: 50, h: 30 } };
  const inner = clip({ box: { x: 0, y: 0, w: 200, h: 100 }, maxScrollTop: 400 });
  const outer = clip({ desc: "div.pane", overflowY: "clip", box: { x: 0, y: 0, w: 300, h: 120 } });
  assert.deepEqual(checkClipping({ ...element, chain: [inner, outer] }), []);
  // But a list that itself sticks out of a clipping pane is cut there.
  const list = { desc: "list", rect: { x: 0, y: 0, w: 200, h: 200 }, chain: [outer] };
  assert.deepEqual(rules(checkClipping(list)), ["clipped-by-ancestor"]);
});

test("overlays and focus rings may never be cut, even by a scroll container", () => {
  const element = { desc: "menu", rect: { x: 10, y: 90, w: 50, h: 30 }, chain: [clip({ maxScrollTop: 400 })] };
  assert.deepEqual(rules(checkClipping(element, { strict: true, rule: "floating-clipped" })), ["floating-clipped"]);
});

test("sub-pixel overhang is tolerated", () => {
  assert.deepEqual(checkClipping({ desc: "b", rect: { x: 0, y: 0, w: 200.6, h: 100.6 }, chain: [clip({ overflowY: "hidden" })] }), []);
});

test("truncated text needs an ellipsis and a way to read the full value", () => {
  const base = { desc: "span.title", text: "A long title", rect: { x: 0, y: 0, w: 50, h: 20 }, overflowX: 80, overflowY: 0, styleOverflowX: "hidden", styleOverflowY: "hidden", textOverflow: "clip", lineClamp: null, affordance: null, spill: null };
  assert.deepEqual(rules(checkTruncation(base)), ["text-cut-without-ellipsis"]);
  assert.deepEqual(rules(checkTruncation({ ...base, textOverflow: "ellipsis" })), ["truncated-text-unreadable"]);
  assert.deepEqual(checkTruncation({ ...base, textOverflow: "ellipsis", affordance: "title" }), []);
  assert.deepEqual(checkTruncation({ ...base, overflowX: 0, overflowY: 40, lineClamp: "2", affordance: "data-tip" }), []);
  // A scroll container keeps its text reachable.
  assert.deepEqual(checkTruncation({ ...base, styleOverflowX: "auto" }), []);
});

test("glyphs hanging out of a tight line box are not lost text", () => {
  const tight = { desc: "strong", text: "Drop books here", rect: { x: 0, y: 0, w: 200, h: 18 }, overflowX: 0, overflowY: 2, styleOverflowX: "visible", styleOverflowY: "visible", textOverflow: "clip", lineClamp: null, lineHeight: 18, affordance: null, spill: { right: 0, bottom: 2 } };
  assert.deepEqual(checkTruncation(tight), []);
  assert.deepEqual(checkTruncation({ ...tight, styleOverflowX: "hidden", styleOverflowY: "hidden", spill: null }), []);
  // A whole hidden line is.
  assert.deepEqual(rules(checkTruncation({ ...tight, overflowY: 18, styleOverflowY: "hidden", spill: null })), ["text-cut-without-ellipsis"]);
});

test("text that runs out of its box is reported", () => {
  const spill = { desc: "h2", text: "x".repeat(200), rect: { x: 0, y: 0, w: 50, h: 20 }, overflowX: 900, overflowY: 0, styleOverflowX: "visible", styleOverflowY: "visible", textOverflow: "clip", lineClamp: null, affordance: null, spill: { right: 900, bottom: 0 } };
  assert.deepEqual(rules(checkTruncation(spill)), ["text-overflows-box"]);
});

test("fixed layers may cover a control only while it can still be scrolled out", () => {
  const item = { desc: "button.row", primary: false, rect: { x: 0, y: 560, w: 100, h: 40 } };
  const hit = { point: "center", hitDesc: "div.player", overlayDesc: "div.player-bar", overlayRect: { x: 0, y: 570, w: 960, h: 70 }, inPopup: false };
  assert.deepEqual(checkCoverage({ ...item, coverage: { hits: [hit], remainingUp: 0, remainingDown: 200 } }), []);
  assert.deepEqual(rules(checkCoverage({ ...item, coverage: { hits: [hit], remainingUp: 0, remainingDown: 0 } })), ["under-fixed-layer"]);
});

test("open popups may cover the page; plain overlaps may not", () => {
  const item = { desc: "button", primary: false, rect: { x: 0, y: 0, w: 100, h: 40 } };
  const popupHit = { point: "center", hitDesc: "div[role=listbox]", overlayDesc: null, overlayRect: null, inPopup: true };
  assert.deepEqual(checkCoverage({ ...item, coverage: { hits: [popupHit], remainingUp: 0, remainingDown: 0 } }), []);
  const badge = { point: "center", hitDesc: "span.badge", overlayDesc: null, overlayRect: null, inPopup: false };
  assert.deepEqual(rules(checkCoverage({ ...item, coverage: { hits: [badge], remainingUp: 0, remainingDown: 0 } })), ["control-overlapped"]);
  // A corner overlap only matters for a primary action.
  const corner = { ...badge, point: "top-left" };
  assert.deepEqual(checkCoverage({ ...item, coverage: { hits: [corner], remainingUp: 0, remainingDown: 0 } }), []);
  assert.deepEqual(rules(checkCoverage({ ...item, primary: true, coverage: { hits: [corner], remainingUp: 0, remainingDown: 0 } })), ["primary-action-overlapped"]);
});

const style = (outline = "none", shadow = "none", background = "rgb(0, 0, 0)") => [outline, outline === "none" ? "0px" : "2px", "rgb(255, 255, 255)", "2px", shadow, "c", "c", "c", "c", background, "rgb(255, 255, 255)", "none"];

test("focus is visible when the control or its container changes", () => {
  assert.deepEqual(checkFocusVisible({ desc: "b", baseline: [style()], now: [style("solid")] }), []);
  assert.deepEqual(checkFocusVisible({ desc: "b", baseline: [style(), style()], now: [style(), style("none", "none", "rgb(9, 9, 9)")] }), []);
  assert.deepEqual(rules(checkFocusVisible({ desc: "b", baseline: [style()], now: [style()] })), ["focus-not-visible"]);
});

test("a ring drawn on a descendant (a card's cover) counts as visible focus", () => {
  // Entries 0-2 are the box and its ancestors (null past the top), then descendants.
  const baseline = [style(), null, null, style(), style()];
  assert.deepEqual(checkFocusVisible({ desc: "card", baseline, now: [style(), null, null, style(), style("solid")] }), []);
  assert.deepEqual(rules(checkFocusVisible({ desc: "card", baseline, now: [style(), null, null, style(), style()] })), ["focus-not-visible"]);
});

test("without a baseline, focus needs a ring of its own", () => {
  assert.deepEqual(checkFocusVisible({ desc: "b", baseline: null, now: [style("none", "0 0 0 2px white")] }), []);
  assert.deepEqual(rules(checkFocusVisible({ desc: "b", baseline: null, now: [style()] })), ["focus-not-visible"]);
});

test("focus must land on a visible, on-screen, unclipped control", () => {
  const info = { none: false, desc: "b", hidden: false, inViewport: true, rect: { x: 10, y: 10, w: 50, h: 20 }, ring: { x: 6, y: 6, w: 58, h: 28 }, ringGrow: 4, chain: [], coverage: null };
  assert.deepEqual(checkFocusLanding(info, viewport), []);
  assert.deepEqual(rules(checkFocusLanding({ ...info, hidden: true }, viewport)), ["focus-on-invisible"]);
  assert.deepEqual(rules(checkFocusLanding({ ...info, chain: [clip({ box: { x: 8, y: 0, w: 200, h: 100 } })] }, viewport)), ["focus-ring-clipped"]);
  const covered = { hits: [{ point: "center", hitDesc: "div.toast", inPopup: false }] };
  assert.deepEqual(rules(checkFocusLanding({ ...info, coverage: covered }, viewport)), ["focused-control-covered"]);
});

test("a tooltip must not cover the control it explains", () => {
  const anchor = { x: 100, y: 100, w: 40, h: 40 };
  assert.deepEqual(checkTooltip({ desc: "tip", rect: { x: 100, y: 60, w: 80, h: 30 } }, anchor, viewport), []);
  assert.deepEqual(rules(checkTooltip({ desc: "tip", rect: { x: 100, y: 110, w: 80, h: 30 } }, anchor, viewport)), ["tooltip-covers-anchor"]);
});

test("only moves that the action caused, and undid, count as layout shift", () => {
  const before = { 1: { x: 0, y: 0, w: 10, h: 10 }, 2: { x: 0, y: 20, w: 10, h: 10 }, 3: { x: 0, y: 40, w: 10, h: 10 } };
  const during = { 1: { x: 0, y: 0, w: 12, h: 10 }, 2: { x: 0, y: 24, w: 10, h: 10 }, 3: { x: 5, y: 40, w: 10, h: 10 } };
  // Element 3 drifted on its own (live content) and did not come back.
  const restored = { 1: before[1], 2: before[2], 3: { x: 9, y: 40, w: 10, h: 10 } };
  assert.deepEqual(layoutShifts(before, during, restored, { ignore: new Set([1]) }).map((shift) => shift.id), [2]);
});

test("sideways page scroll is a failure", () => {
  assert.deepEqual(checkHorizontalScroll({ horizontalScroll: 0 }), []);
  assert.deepEqual(rules(checkHorizontalScroll({ horizontalScroll: 24 })), ["horizontal-page-scroll"]);
});

test("a defect is reported once per state, window and target", () => {
  const failure = { state: "s", viewport: "960x640", rule: "r", target: "t", detail: "first" };
  assert.equal(failureKey(failure), failureKey({ ...failure, detail: "second" }));
  assert.notEqual(failureKey(failure), failureKey({ ...failure, viewport: "1440x900" }));
});

test("a focus ring must be 2px and change 85% of its band with 3:1 contrast", () => {
  const box = { x: 0, y: 0, w: 100, h: 36 };
  const ring = { width: 2, offset: -3, radius: 0 };
  const band = ringBandArea(box, ring);
  assert.equal(band, 98 * 34 - 94 * 30); // a 2px ring 1px inside the edge
  assert.deepEqual(checkFocusIndicator({ desc: "button", box, ring, changed3: Math.ceil(band * 0.9) }), []);
  // A faint ring (8% white) changes almost no pixels by 3:1.
  assert.deepEqual(rules(checkFocusIndicator({ desc: "button", box, ring, changed3: 12 })), ["focus-indicator-faint"]);
  assert.deepEqual(rules(checkFocusIndicator({ desc: "button", box, ring: { ...ring, width: 1 }, changed3: band })), ["focus-indicator-thin"]);
  // Rounded corners shrink the band, so a pill-shaped ring is not penalised.
  assert.ok(ringBandArea({ x: 0, y: 0, w: 40, h: 40 }, { width: 2, offset: -3, radius: 20 }) < ringBandArea({ x: 0, y: 0, w: 40, h: 40 }, { width: 2, offset: -3, radius: 0 }));
});

test("text clipped by an ancestor or covered by an opaque layer is reported", () => {
  const text = { desc: "span.meta", text: "Audiobook · 3 chapters", rect: { x: 0, y: 0, w: 90, h: 18 } };
  const clipped = { ...text, textRect: { x: 0, y: 0, w: 160, h: 18 }, chain: [clip({ overflowX: "hidden", box: { x: 0, y: 0, w: 90, h: 40 } })] };
  assert.deepEqual(rules(checkTextClipping(clipped)), ["text-clipped-by-ancestor"]);
  assert.deepEqual(checkTextClipping({ ...text, textRect: null, chain: null }), []);
  const covered = { ...text, coverage: { hits: [{ point: "center", hitDesc: "div.cover", overlayDesc: null, overlayRect: null, inPopup: false }], remainingUp: 0, remainingDown: 0 } };
  assert.deepEqual(rules(checkTextCoverage(covered)), ["text-covered"]);
});

test("a tooltip's own text must not be cut", () => {
  const anchor = { x: 100, y: 100, w: 40, h: 40 };
  assert.deepEqual(rules(checkTooltip({ desc: "tip", rect: { x: 100, y: 60, w: 70, h: 30 }, textCut: true }, anchor, viewport)), ["tooltip-text-cut"]);
});

test("forced colours: labels must be drawn and selected states must show", () => {
  assert.deepEqual(rules(checkForcedLabel({ glyphPixels: 0, box: { x: 0, y: 0, w: 40, h: 16 } }, "button.btn-primary", "dark")), ["label-invisible-in-forced-colors"]);
  assert.deepEqual(checkForcedLabel({ glyphPixels: 120 }, "button", "dark"), []);
  assert.deepEqual(rules(checkForcedState({ markerPixels: 0 }, "button.rail-tab.active", "light")), ["selected-state-invisible-in-forced-colors"]);
  assert.deepEqual(checkForcedState({ markerPixels: 48 }, "button.rail-tab.active", "light"), []);
});

test("a fixed bar over a scroll area's edge needs that much padding reserved", () => {
  const area = { desc: "section.main-workspace", barDesc: "footer.player-bar", edge: "bottom", overlap: { x: 264, y: 778, w: 928, h: 82 }, reserved: 24 };
  assert.deepEqual(rules(checkScrollAreaUnderBar(area)), ["scroll-area-under-fixed-layer"]);
  assert.deepEqual(checkScrollAreaUnderBar({ ...area, reserved: 82 }), []);
  assert.deepEqual(checkScrollAreaUnderBar({ ...area, overlap: { ...area.overlap, h: 1 }, reserved: 0 }), []);
});

test("a hover tip is waited out unless it is the focused control's own", () => {
  assert.equal(hoverTipLingers({ shown: null, focusedTip: null }), false);
  assert.equal(hoverTipLingers({ shown: "A long title", focusedTip: null }), true);
  // Focus on a control with a tip of its own (a dialog's Close button) says
  // nothing about some other control's tip still showing.
  assert.equal(hoverTipLingers({ shown: "A long title", focusedTip: "Close (Esc)" }), true);
  assert.equal(hoverTipLingers({ shown: "Close (Esc)", focusedTip: "Close (Esc)" }), false);
  // Cut-short text is shown ahead of the control's tip.
  assert.equal(hoverTipLingers({ shown: "A long title · Close (Esc)", focusedTip: " Close (Esc) " }), false);
});

test("controls rebuilt by closing and reopening a dialog pair with their copies", () => {
  const before = [
    { id: 1, desc: "button \"Close\"" }, { id: 2, desc: "button.row \"Part 1\"" }, { id: 3, desc: "button.row \"Part 1\"" },
    { id: 4, desc: "button#toggle \"Now playing\"" },
  ];
  const after = [
    { id: 11, desc: "button \"Close\"" }, { id: 12, desc: "button.row \"Part 1\"" }, { id: 13, desc: "button.row \"Part 1\"" },
    { id: 4, desc: "button#toggle \"Now playing\"" },
  ];
  const { twins, unmatched } = rebuiltTwins(before, after);
  // Same description twice: paired in document order, never both to one original.
  assert.deepEqual([...twins], [[11, 1], [12, 2], [13, 3]]);
  assert.deepEqual(unmatched, []);
  // A control that stayed is nobody's copy; a new control with no counterpart
  // is reported, so it still has to be reached with Tab.
  const grown = rebuiltTwins(before, [...after, { id: 14, desc: "button \"Extra\"" }, { id: 15, desc: "button \"Close\"" }]);
  assert.equal(grown.twins.has(4), false);
  assert.deepEqual(grown.unmatched, [14, 15]);
  assert.deepEqual(rebuiltTwins(before, before), { twins: new Map(), unmatched: [] });
});

// Pure pass/fail rules for interaction-audit.mjs. The browser probe
// (interactionProbe.mjs) only measures; every judgement is made here so each
// rule, and each documented exception to it, has a unit test.

/** Sub-pixel layout noise that no user can see. */
const TOLERANCE_PX = 1;

export const VIEWPORTS = Object.freeze([
  Object.freeze({ width: 960, height: 640 }),
  Object.freeze({ width: 1200, height: 860 }),
  Object.freeze({ width: 1440, height: 900 }),
  Object.freeze({ width: 1920, height: 1080 }),
]);

const AUDIT_FLAGS = new Set(["out", "viewport", "state", "list-states", "headed", "quick"]);

/** Parse `--name=value` flags; bare `--flag` becomes true. */
export function parseArgs(argv) {
  const options = {};
  for (const arg of argv) {
    const match = /^--([a-z][a-z0-9-]*)(?:=(.*))?$/.exec(arg);
    if (!match || !AUDIT_FLAGS.has(match[1])) throw new Error(`Unknown argument: ${arg}`);
    options[match[1]] = match[2] ?? true;
  }
  return options;
}

/** `--viewport=960x640,1440` selects by size or width; default is all four. */
export function selectViewports(filter, available = VIEWPORTS) {
  if (!filter || filter === true) return [...available];
  const wanted = String(filter).split(",").map((value) => value.trim()).filter(Boolean);
  const selected = available.filter((viewport) => wanted.some((value) =>
    value === `${viewport.width}x${viewport.height}` || value === String(viewport.width)));
  const unknown = wanted.filter((value) => !available.some((viewport) =>
    value === `${viewport.width}x${viewport.height}` || value === String(viewport.width)));
  // Any other explicit WIDTHxHEIGHT is audited as given (probing a minimum size).
  for (const value of [...unknown]) {
    const match = /^(\d{3,4})x(\d{3,4})$/.exec(value);
    if (!match) continue;
    selected.push({ width: Number(match[1]), height: Number(match[2]) });
    unknown.splice(unknown.indexOf(value), 1);
  }
  if (unknown.length || !selected.length) {
    throw new Error(`Unknown --viewport ${unknown.join(", ") || filter}; choose from ${available.map((v) => `${v.width}x${v.height}`).join(", ")}`);
  }
  return selected;
}

/** `--state=a,b*` selects states by exact id or trailing-* prefix. */
export function selectStates(filter, ids) {
  if (!filter || filter === true) return [...ids];
  const patterns = String(filter).split(",").map((value) => value.trim()).filter(Boolean);
  const matches = (id, pattern) => (pattern.endsWith("*") ? id.startsWith(pattern.slice(0, -1)) : id === pattern);
  const unknown = patterns.filter((pattern) => !ids.some((id) => matches(id, pattern)));
  if (unknown.length) throw new Error(`Unknown --state ${unknown.join(", ")}; available: ${ids.join(", ")}`);
  return ids.filter((id) => patterns.some((pattern) => matches(id, pattern)));
}

/** How far `inner` sticks out of `outer` on each side (positive = outside). */
export function overhang(inner, outer) {
  return {
    left: outer.x - inner.x,
    top: outer.y - inner.y,
    right: inner.x + inner.w - (outer.x + outer.w),
    bottom: inner.y + inner.h - (outer.y + outer.h),
  };
}

function intersection(a, b) {
  const x = Math.max(a.x, b.x);
  const y = Math.max(a.y, b.y);
  const right = Math.min(a.x + a.w, b.x + b.w);
  const bottom = Math.min(a.y + a.h, b.y + b.h);
  return right > x && bottom > y ? { x, y, w: right - x, h: bottom - y } : null;
}

function intersects(a, b, tolerance = 0) {
  return a.x + a.w - tolerance > b.x && b.x + b.w - tolerance > a.x
    && a.y + a.h - tolerance > b.y && b.y + b.h - tolerance > a.y;
}

function sidesOutside(hang, tolerance = TOLERANCE_PX) {
  return Object.entries(hang).filter(([, amount]) => amount > tolerance).map(([side, amount]) => `${side} ${Math.round(amount)}px`);
}

/** A tooltip, menu, dialog, toast or other fixed layer must be fully on screen. */
export function checkFloatingInViewport(popup, viewport) {
  const outside = sidesOutside(overhang(popup.rect, { x: 0, y: 0, w: viewport.cw ?? viewport.w, h: viewport.ch ?? viewport.h }));
  return outside.length ? [{ rule: "floating-outside-viewport", target: popup.desc, detail: `outside the window: ${outside.join(", ")}`, rect: popup.rect }] : [];
}

/**
 * Is `rect` cut off by a clipping ancestor?
 *
 * A control scrolled out of a scroll container is fine when scrolling that
 * container in the clipped direction can reveal it. Overlays (`strict`) and
 * focus rings are never allowed to be cut: they cannot be scrolled into view.
 */
export function checkClipping({ rect, chain, desc }, { strict = false, rule = "clipped-by-ancestor", tolerance = TOLERANCE_PX } = {}) {
  const failures = [];
  // Walk outwards. An outer ancestor only clips what the inner ones leave
  // visible, so a row scrolled out of an inner list is judged by that list
  // alone, not again by every pane around it.
  let visible = rect;
  for (const clip of chain) {
    if (!visible) break;
    const hang = overhang(visible, clip.box);
    visible = intersection(visible, clip.box);
    const cut = [];
    for (const [side, amount] of Object.entries(hang)) {
      if (amount <= tolerance) continue;
      const axisOverflow = side === "left" || side === "right" ? clip.overflowX : clip.overflowY;
      const scrollable = /^(auto|scroll|overlay)$/.test(axisOverflow);
      if (!strict && scrollable) {
        const remaining = side === "left" ? clip.scrollLeft
          : side === "right" ? clip.maxScrollLeft - clip.scrollLeft
            : side === "top" ? clip.scrollTop
              : clip.maxScrollTop - clip.scrollTop;
        if (remaining + tolerance >= amount) continue;
      }
      cut.push(`${side} ${Math.round(amount)}px`);
    }
    if (cut.length) {
      failures.push({ rule, target: desc, detail: `cut off by ${clip.desc} (overflow ${clip.overflowX}/${clip.overflowY}): ${cut.join(", ")}`, rect });
    }
  }
  return failures;
}

/**
 * Text that overflows its box must either show an ellipsis (or line clamp)
 * and offer the full value through a title or tooltip, or scroll.
 */
export function checkTruncation(text) {
  const failures = [];
  // A tight line-height lets ascenders and descenders hang a few pixels out of
  // the line box; that loses no text. Vertically, only a hidden or spilling
  // part of at least half a line counts.
  const verticalLimit = Math.max(TOLERANCE_PX, (text.lineHeight ?? 0) / 2);
  const clipsX = text.overflowX > TOLERANCE_PX && /^(hidden|clip)$/.test(text.styleOverflowX);
  const clipsY = text.overflowY > verticalLimit && /^(hidden|clip)$/.test(text.styleOverflowY);
  if (text.spill && (text.spill.right > TOLERANCE_PX || text.spill.bottom > verticalLimit)) {
    failures.push({ rule: "text-overflows-box", target: text.desc, detail: `text runs ${text.spill.right > 1 ? `${text.spill.right}px past the right edge` : `${text.spill.bottom}px past the bottom edge`} of its box: "${text.text}"`, rect: text.rect });
  }
  if (!clipsX && !clipsY) return failures;
  const marked = (clipsX && text.textOverflow === "ellipsis") || (clipsY && text.lineClamp) || (clipsX && text.lineClamp);
  if (!marked) {
    failures.push({ rule: "text-cut-without-ellipsis", target: text.desc, detail: `text is cut ${clipsX ? `${Math.round(text.overflowX)}px horizontally` : `${Math.round(text.overflowY)}px vertically`} with no ellipsis: "${text.text}"`, rect: text.rect });
  } else if (!text.affordance) {
    // `textId` lets the audit check whether hovering reveals the full value.
    failures.push({ rule: "truncated-text-unreadable", target: text.desc, detail: `ellipsis without a title or tooltip carrying the full text: "${text.text}"`, rect: text.rect, textId: text.id, fullText: text.text });
  }
  return failures;
}

/**
 * Coverage of a control by something else, from centre/corner hit tests.
 *
 * - An open popup is meant to cover the page below it: ignored.
 * - A fixed or sticky layer (player bar, sticky footer, toast) may cover a
 *   control only while the control's scroll container can still move it out.
 * - Anything else covering the centre of a control, or any point of a primary
 *   action, is a failure.
 */
export function checkCoverage(item) {
  const coverage = item.coverage;
  if (!coverage) return [];
  const failures = [];
  const hits = coverage.hits.filter((hit) => !hit.inPopup);
  const overlayHits = hits.filter((hit) => hit.overlayDesc);
  const reported = new Set();
  for (const hit of overlayHits) {
    if (reported.has(hit.overlayDesc)) continue;
    const overlay = hit.overlayRect;
    const below = overlay.y + overlay.h / 2 > item.rect.y + item.rect.h / 2;
    const needed = below ? item.rect.y + item.rect.h - overlay.y : overlay.y + overlay.h - item.rect.y;
    const remaining = below ? coverage.remainingDown : coverage.remainingUp;
    if (remaining + TOLERANCE_PX >= needed) continue;
    reported.add(hit.overlayDesc);
    failures.push({ rule: "under-fixed-layer", target: item.desc, detail: `sits under ${hit.overlayDesc} at its ${hit.point} and cannot be scrolled out (needs ${Math.round(needed)}px, can scroll ${Math.round(remaining)}px)`, rect: item.rect });
  }
  const plainHits = hits.filter((hit) => !hit.overlayDesc && (item.primary || hit.point === "center"));
  if (plainHits.length) {
    const hit = plainHits[0];
    failures.push({ rule: item.primary ? "primary-action-overlapped" : "control-overlapped", target: item.desc, detail: `${hit.hitDesc} covers its ${plainHits.map((value) => value.point).join(", ")}`, rect: item.rect });
  }
  return failures;
}

const OUTLINE_STYLE = 0;
const OUTLINE_WIDTH = 1;
const BOX_SHADOW = 4;

/** True when the computed style shows a focus ring of its own. */
function hasRing(style) {
  return (style[OUTLINE_STYLE] !== "none" && parseFloat(style[OUTLINE_WIDTH]) >= 1)
    || (style[BOX_SHADOW] && style[BOX_SHADOW] !== "none");
}

/**
 * Focus is visible when the focused box, its parent or grandparent (for
 * `:focus-within` designs) or one of its first descendants (a card that rings
 * its cover) changes its ring, border, background or text. The probe reports
 * outline values as "none" while the outline style is none, so an invisible
 * offset change does not count. With no unfocused baseline, a ring is required.
 */
export function checkFocusVisible(info) {
  if (info.baseline) {
    const changed = info.now.some((style, depth) => {
      const before = info.baseline[depth];
      return before && style && style.some((value, index) => value !== before[index]);
    });
    if (changed) return [];
    return [{ rule: "focus-not-visible", target: info.desc, detail: "keyboard focus does not change the outline, shadow, border, background or text of the control or its container", rect: info.rect }];
  }
  if (info.now.some((style) => style && hasRing(style))) return [];
  return [{ rule: "focus-not-visible", target: info.desc, detail: "keyboard focus shows no outline or shadow", rect: info.rect }];
}

/** Keyboard focus must land on something visible, on screen and uncovered. */
export function checkFocusLanding(info, viewport) {
  const failures = [];
  if (info.none) return failures;
  if (info.hidden) failures.push({ rule: "focus-on-invisible", target: info.desc, detail: "focus landed on an element with no visible box", rect: info.rect });
  else if (!info.inViewport) failures.push({ rule: "focus-off-screen", target: info.desc, detail: "focused control is outside the window", rect: info.rect });
  else {
    const outside = sidesOutside(overhang(info.rect, { x: 0, y: 0, w: viewport.cw ?? viewport.w, h: viewport.ch ?? viewport.h }));
    if (outside.length) failures.push({ rule: "focus-off-screen", target: info.desc, detail: `focused control is partly outside the window: ${outside.join(", ")}`, rect: info.rect });
  }
  if (!info.hidden && info.ringGrow > 0) {
    failures.push(...checkClipping({ rect: info.ring, chain: info.chain, desc: info.desc }, { strict: true, rule: "focus-ring-clipped" }));
  } else if (!info.hidden) {
    failures.push(...checkClipping({ rect: info.rect, chain: info.chain, desc: info.desc }, { strict: true, rule: "focused-control-clipped" }));
  }
  if (info.coverage) {
    const hits = info.coverage.hits.filter((hit) => !hit.inPopup && hit.point === "center");
    if (hits.length) failures.push({ rule: "focused-control-covered", target: info.desc, detail: `${hits[0].hitDesc} covers the focused control`, rect: info.rect });
  }
  return failures;
}

/** A tooltip must be on screen and must not cover the control it explains. */
export function checkTooltip(popup, anchorRect, viewport) {
  const failures = checkFloatingInViewport(popup, viewport);
  if (anchorRect && intersects(popup.rect, anchorRect, TOLERANCE_PX)) {
    failures.push({ rule: "tooltip-covers-anchor", target: popup.desc, detail: "the tooltip covers the control it belongs to", rect: popup.rect });
  }
  failures.push(...checkTooltipText(popup));
  return failures;
}

/** A tooltip exists to show text in full; its own text must not be cut. */
function checkTooltipText(popup) {
  return popup.textCut
    ? [{ rule: "tooltip-text-cut", target: popup.desc, detail: "the tooltip's own text is cut off (ellipsis or clipped overflow)", rect: popup.rect }]
    : [];
}

/**
 * Text drawn outside a clipping ancestor (a label in a narrow overflow:hidden
 * row), unless that ancestor can scroll it into view.
 */
export function checkTextClipping(text) {
  if (!text.textRect || !text.chain) return [];
  return checkClipping({ rect: text.textRect, chain: text.chain, desc: text.desc }, { rule: "text-clipped-by-ancestor" })
    .map((failure) => ({ ...failure, detail: `${failure.detail}: "${text.text}"` }));
}

/**
 * Text covered by an opaque layer: the same rules as for controls (fixed
 * layers only if the text cannot be scrolled out from under them; open popups
 * are meant to cover), with text-specific rule names.
 */
export function checkTextCoverage(text) {
  if (!text.coverage) return [];
  return checkCoverage({ desc: text.desc, primary: false, rect: text.rect, coverage: text.coverage })
    .map((failure) => ({ ...failure, rule: failure.rule === "under-fixed-layer" ? "text-under-fixed-layer" : "text-covered", detail: `${failure.detail}: "${text.text}"` }));
}

/** Pixels of a 2px band around a box inflated by `offset + width`, minus rounded corners. */
export function ringBandArea(box, { width, offset, radius = 0 }) {
  const outer = { w: box.w + 2 * (offset + width), h: box.h + 2 * (offset + width) };
  const inner = { w: box.w + 2 * offset, h: box.h + 2 * offset };
  if (outer.w <= 0 || outer.h <= 0) return 0;
  const cornerLoss = (4 - Math.PI) * Math.min(radius, outer.w / 2, outer.h / 2) ** 2 - (4 - Math.PI) * Math.max(0, Math.min(radius - width, inner.w / 2, inner.h / 2)) ** 2;
  return Math.max(0, outer.w * outer.h - Math.max(0, inner.w) * Math.max(0, inner.h) - cornerLoss);
}

/**
 * The focus indicator, measured in pixels (WCAG 2.4.11 / 1.4.11): a ring at
 * least 2 CSS px thick whose pixels change with at least 3:1 contrast between
 * the unfocused and focused screenshots over 85% of its band. An indicator
 * that is not a ring (a background or text change) must change at least as
 * many pixels as a 2px band around the box.
 */
const RING_COVERAGE = 0.85;
export function checkFocusIndicator(measure) {
  if (!measure) return [];
  const failures = [];
  if (measure.ring && measure.ring.width < 2) {
    failures.push({ rule: "focus-indicator-thin", target: measure.desc, detail: `the focus ring is ${measure.ring.width}px thick; at least 2px is needed`, rect: measure.box });
  }
  const band = measure.ring
    ? ringBandArea(measure.box, measure.ring)
    : ringBandArea(measure.box, { width: 2, offset: -2, radius: measure.radius ?? 0 });
  if (band > 0 && measure.changed3 < RING_COVERAGE * band) {
    failures.push({
      rule: "focus-indicator-faint",
      target: measure.desc,
      detail: `only ${measure.changed3} pixels change with 3:1 contrast when focused; a 2px indicator around this box needs ${Math.round(RING_COVERAGE * band)}`,
      rect: measure.box,
    });
  }
  return failures;
}

function moved(a, b, tolerance) {
  return Math.abs(a.x - b.x) > tolerance || Math.abs(a.y - b.y) > tolerance
    || Math.abs(a.w - b.w) > tolerance || Math.abs(a.h - b.h) > tolerance;
}

/**
 * Elements that moved when the user only hovered, focused, or opened an
 * overlay. A move counts only if the element returns to its original box once
 * the action is undone (`restored`); live content that drifts on its own
 * (progress text, clocks) does not return and is therefore not blamed on the
 * action. `ignore` holds the acted-on element and anything inside it.
 */
export function layoutShifts(before, during, restored, { ignore = new Set(), tolerance = 0.5 } = {}) {
  const shifts = [];
  for (const [id, box] of Object.entries(before)) {
    if (ignore.has(Number(id))) continue;
    const now = during[id];
    if (!now || !moved(box, now, tolerance)) continue;
    const back = restored?.[id];
    if (restored && (!back || moved(box, back, tolerance))) continue;
    shifts.push({ id: Number(id), before: box, during: now });
  }
  return shifts;
}

export function checkHorizontalScroll(page) {
  return page.horizontalScroll > TOLERANCE_PX
    ? [{ rule: "horizontal-page-scroll", target: "document", detail: `the page scrolls sideways by ${page.horizontalScroll}px` }]
    : [];
}

/** Every rule over one full snapshot of the page. */
export function checkSnapshot(snapshot) {
  const failures = [...checkHorizontalScroll(snapshot.page)];
  for (const popup of snapshot.popups) {
    failures.push(...checkFloatingInViewport(popup, snapshot.page.viewport), ...checkTooltipText(popup));
    if (popup.chain.length) failures.push(...checkClipping(popup, { strict: true, rule: "floating-clipped" }));
  }
  for (const element of snapshot.elements) {
    if (element.hidden || element.inert) continue;
    failures.push(...checkClipping(element));
    failures.push(...checkCoverage(element));
  }
  for (const text of snapshot.texts) failures.push(...checkTruncation(text), ...checkTextClipping(text), ...checkTextCoverage(text));
  for (const area of snapshot.scrollAreas ?? []) failures.push(...checkScrollAreaUnderBar(area));
  return failures;
}

/**
 * A fixed bar over the top or bottom edge of a scroll area hides whatever is
 * scrolled to that edge (the last book, the last log line), even when nothing
 * is there right now. The area must reserve at least the overlap as padding.
 */
export function checkScrollAreaUnderBar(area) {
  if (area.overlap.h <= area.reserved + TOLERANCE_PX) return [];
  return [{
    rule: "scroll-area-under-fixed-layer",
    target: area.desc,
    detail: `${area.barDesc} covers its ${area.edge} ${Math.round(area.overlap.h)}px and it reserves ${Math.round(area.reserved)}px: content scrolled to that edge stays hidden`,
    rect: area.overlap,
  }];
}

/** Same defect, same place: report once per state and viewport. */
export function failureKey(failure) {
  return `${failure.state}|${failure.viewport}|${failure.rule}|${failure.target}`;
}

/**
 * Whether a hover tooltip is still up after the pointer left its control. The
 * tip of the focused control stays for as long as focus does, so that one is
 * not waited out; any other tip is the previous control's, inside its grace
 * period, and would sit under the next pointer position.
 */
export function hoverTipLingers({ shown, focusedTip }) {
  if (shown === null || shown === undefined) return false;
  const own = focusedTip?.trim();
  return !(own && shown.includes(own));
}

/**
 * Controls that were replaced by an identical copy between two readings of the
 * page (a dialog closed and reopened by its toggle is built again from new
 * nodes). Pairs the nth vanished control of a description with the nth new one
 * of the same description, in document order. Returns the pairs as a map from
 * the new id to the old one, and the new controls that have no counterpart.
 */
export function rebuiltTwins(before, after) {
  const afterIds = new Set(after.map((element) => element.id));
  const beforeIds = new Set(before.map((element) => element.id));
  const vanished = new Map();
  for (const element of before) {
    if (afterIds.has(element.id)) continue;
    const queue = vanished.get(element.desc) ?? [];
    queue.push(element.id);
    vanished.set(element.desc, queue);
  }
  const twins = new Map();
  const unmatched = [];
  for (const element of after) {
    if (beforeIds.has(element.id)) continue;
    const old = vanished.get(element.desc)?.shift();
    if (old === undefined) unmatched.push(element.id);
    else twins.set(element.id, old);
  }
  return { twins, unmatched };
}

/** Glyph pixels below this mean a forced-colours label is not drawn (a block or nothing). */
const MIN_LABEL_GLYPH_PIXELS = 10;
/** Marker pixels below this mean a selected control looks like any other. */
const MIN_STATE_MARKER_PIXELS = 12;

/** In forced colours, a control's text must be drawn against what is behind it. */
export function checkForcedLabel(measure, desc, scheme) {
  if (!measure || measure.glyphPixels >= MIN_LABEL_GLYPH_PIXELS) return [];
  return [{ rule: "label-invisible-in-forced-colors", target: desc, detail: `in forced colours (${scheme}) its label shows ${measure.glyphPixels} pixels at 3:1 against the colour behind it: the text is not readable`, rect: measure.box }];
}

/** In forced colours, a selected or current control must look different from its siblings. */
export function checkForcedState(measure, desc, scheme) {
  if (!measure || measure.markerPixels >= MIN_STATE_MARKER_PIXELS) return [];
  return [{ rule: "selected-state-invisible-in-forced-colors", target: desc, detail: `in forced colours (${scheme}) the selected control has ${measure.markerPixels} pixels of any colour its unselected sibling lacks` }];
}

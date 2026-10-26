/**
 * Keeps keyboard focus from falling back to <body>.
 *
 * Browsers drop focus to the document when the focused control disappears:
 * it unmounts (a removed queue row, a finished job's button), is hidden (Start
 * giving way to Cancel), becomes disabled (a busy Save), or its whole view is
 * replaced. A keyboard user is then lost: the next Tab starts from the top.
 *
 * One listener handles every case. When focus leaves a control for nowhere,
 * and not because the user clicked or switched windows, it is put back:
 *  - on the same control, if it is still usable, or once it is re-enabled
 *    within a short busy period;
 *  - otherwise on the usable control nearest to where the lost one was,
 *    inside the closest surviving container (never outside an open modal);
 *  - when the whole view was replaced, on the element marked
 *    `data-focus-landing` (the page heading, which shows while focused).
 * Code that moves focus itself (a dialog returning focus to its opener) runs
 * first; the rescue only acts if focus is still on <body> afterwards.
 */

const FOCUSABLE = [
  "a[href]", "button", "input:not([type='hidden'])", "select", "textarea", "summary",
  "[tabindex]:not([tabindex='-1'])", "[contenteditable='true']",
].join(",");

/**
 * How long a control that disabled itself (busy) may take to come back. Focus
 * sits on the page meanwhile, so this stays short: a local request that takes
 * longer hands focus to the nearest control instead.
 */
const RE_ENABLE_WAIT_MS = 600;

function shown(element: Element): boolean {
  if (!element.isConnected || element.closest("[hidden], [inert]")) return false;
  return typeof element.checkVisibility === "function" ? element.checkVisibility() : true;
}

function usable(element: Element | null): boolean {
  return element instanceof HTMLElement && shown(element) && !element.matches(":disabled");
}

function center(rect: DOMRect | { left: number; top: number; width: number; height: number }) {
  return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
}

/** The usable focusable inside `container` nearest to `from`. */
function nearestFocusable(container: Element, from: { left: number; top: number; width: number; height: number }, exclude?: Element): HTMLElement | null {
  const origin = center(from);
  let best: HTMLElement | null = null;
  let bestDistance = Infinity;
  for (const candidate of container.querySelectorAll<HTMLElement>(FOCUSABLE)) {
    if (candidate === exclude || !usable(candidate)) continue;
    const point = center(candidate.getBoundingClientRect());
    const distance = Math.hypot(point.x - origin.x, point.y - origin.y);
    if (distance < bestDistance) {
      best = candidate;
      bestDistance = distance;
    }
  }
  return best;
}

export function installFocusRescue(doc: Document = document): () => void {
  let last: HTMLElement | null = null;
  let lastRect = { left: 0, top: 0, width: 0, height: 0 };
  let chain: Element[] = [];
  // A pointer press since the last focus means the user moved focus on purpose.
  let pointerSinceFocus = false;
  // Tab past the last control leaves the page on purpose, too.
  let tabSinceFocus = false;
  // Whether the lost control was disabled when focus fell: a busy blip.
  let lostWhileDisabled = false;
  let cancelWait: (() => void) | null = null;
  let frame = 0;

  const lostFocus = () => {
    const active = doc.activeElement;
    return !active || active === doc.body || active === doc.documentElement;
  };

  // Each rescue is announced as a "focusrescue" event (kind: refocus, re-enabled,
  // nearest, landing), so tests and the interaction audit can see that focus
  // fell, and why, even though the user never notices.
  const report = (kind: string, target: Element) => {
    doc.dispatchEvent(new CustomEvent("focusrescue", { detail: { kind, target } }));
  };
  const land = (target: HTMLElement | null, kind = "nearest") => {
    if (target && lostFocus()) {
      target.focus();
      report(kind, target);
    }
  };

  const fallback = (lost: HTMLElement) => {
    // An open modal keeps focus inside itself, even when the lost control was in it.
    const modal = chain.find((element) => element.matches("[aria-modal='true']") && shown(element));
    if (modal) {
      land(nearestFocusable(modal, lastRect, lost));
      return;
    }
    const root = doc.getElementById("root");
    for (const container of chain) {
      if (container === doc.body || container === doc.documentElement || container === root) break;
      if (!shown(container)) continue;
      const target = nearestFocusable(container, lastRect, lost);
      if (target) {
        land(target);
        return;
      }
    }
    // The whole view was replaced: go to its landing point.
    const landing = doc.querySelector<HTMLElement>("[data-focus-landing]");
    land(landing && shown(landing) ? landing : nearestFocusable(doc.body, lastRect, lost), "landing");
  };

  const rescue = () => {
    const lost = last;
    if (!lost || cancelWait || pointerSinceFocus || tabSinceFocus || !lostFocus() || !doc.hasFocus()) return;
    if (usable(lost)) {
      // Back already: a busy control that disabled itself for a moment, or the
      // control is simply still there and something dropped focus by mistake
      // ("refocus", reported because that is a bug elsewhere).
      land(lost, lostWhileDisabled ? "re-enabled" : "refocus");
      return;
    }
    if (lost.isConnected && shown(lost) && lost.matches(":disabled")) {
      // A busy control usually comes back; wait for it before moving on.
      const observer = new MutationObserver(() => {
        if (!lost.matches(":disabled")) {
          done();
          land(lost, "re-enabled");
        }
      });
      const timer = window.setTimeout(() => {
        done();
        if (lostFocus()) fallback(lost);
      }, RE_ENABLE_WAIT_MS);
      const done = () => {
        observer.disconnect();
        window.clearTimeout(timer);
        cancelWait = null;
      };
      cancelWait = done;
      observer.observe(lost, { attributes: true, attributeFilter: ["disabled"] });
      return;
    }
    fallback(lost);
  };

  const onFocusIn = (event: FocusEvent) => {
    if (!(event.target instanceof HTMLElement)) return;
    cancelWait?.();
    pointerSinceFocus = false;
    tabSinceFocus = false;
    lostWhileDisabled = false;
    last = event.target;
    const rect = last.getBoundingClientRect();
    lastRect = { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
    chain = [];
    for (let node = last.parentElement; node; node = node.parentElement) chain.push(node);
  };
  // Removing or hiding the focused element fires no blur event, so DOM
  // changes are watched as well as focusout. The check runs two frames later,
  // after any code that moves focus on purpose (a dialog restoring its opener).
  const schedule = () => {
    if (!last || pointerSinceFocus || tabSinceFocus || !lostFocus()) return;
    lostWhileDisabled ||= last.matches(":disabled");
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(rescue);
    });
  };
  const onFocusOut = (event: FocusEvent) => {
    if (!event.relatedTarget && event.target === last) schedule();
  };
  const onPointerDown = () => {
    pointerSinceFocus = true;
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Tab") tabSinceFocus = true;
  };
  const observer = new MutationObserver(schedule);

  doc.addEventListener("focusin", onFocusIn);
  doc.addEventListener("focusout", onFocusOut);
  doc.addEventListener("pointerdown", onPointerDown, true);
  doc.addEventListener("keydown", onKeyDown, true);
  observer.observe(doc.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "hidden", "inert", "class", "style"] });
  return () => {
    observer.disconnect();
    cancelAnimationFrame(frame);
    cancelWait?.();
    doc.removeEventListener("focusin", onFocusIn);
    doc.removeEventListener("focusout", onFocusOut);
    doc.removeEventListener("pointerdown", onPointerDown, true);
    doc.removeEventListener("keydown", onKeyDown, true);
  };
}

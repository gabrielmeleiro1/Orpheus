import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

type Placement = "top" | "bottom" | "left" | "right";

interface ActiveTip {
  anchor: Element;
  text: string;
  preferred: Placement;
  /** Shown for keyboard focus (not hover): it goes when focus leaves the control. */
  viaFocus: boolean;
}

const HOVER_DELAY_MS = 400;
const HOVER_GRACE_MS = 150;
const GAP = 8;
const VIEWPORT_MARGIN = 8;
const OPPOSITE: Record<Placement, Placement> = { top: "bottom", bottom: "top", left: "right", right: "left" };

function tipAnchor(target: EventTarget | null): Element | null {
  return target instanceof Element ? target.closest("[data-tip]") : null;
}

// Controls whose own text may be cut short; their whole (small) subtree is checked.
const TEXT_CONTROL = "button, a[href], summary, label, [role='button'], [role='tab'], [role='option'], [tabindex]:not([tabindex='-1'])";
const ANCESTORS_CHECKED = 3;
// A focusable region (a scrollable text block) is not a small control; only
// its own box is checked, never every node inside it.
const CONTROL_SUBTREE_LIMIT = 40;

function isCutShort(element: HTMLElement): boolean {
  const style = getComputedStyle(element);
  if (style.textOverflow === "ellipsis" && element.scrollWidth > element.clientWidth + 1) return true;
  const clamp = style.getPropertyValue("-webkit-line-clamp");
  return Boolean(clamp && clamp !== "none") && element.scrollHeight > element.clientHeight + 1;
}

/**
 * The full text of ellipsized text in `elements` that offers no other way to
 * read it. Text with its own `title` already has the browser's tooltip.
 */
function cutShortText(elements: Iterable<Element>, root: Element): string | null {
  const texts: string[] = [];
  for (const element of elements) {
    if (!(element instanceof HTMLElement)) continue;
    const titled = element.closest("[title]");
    if (titled && (titled === root || root.contains(titled))) continue;
    if (!isCutShort(element)) continue;
    const text = element.textContent?.replace(/\s+/g, " ").trim();
    if (text && !texts.includes(text)) texts.push(text);
  }
  return texts.length ? texts.join(" · ") : null;
}

/**
 * Where a cut-short text tip belongs for `target`: the control around it (any
 * ellipsized text inside it), or the target or a near ancestor that is itself
 * ellipsized.
 */
export function truncatedTextAnchor(target: EventTarget | null): { anchor: Element; text: string } | null {
  if (!(target instanceof Element)) return null;
  const control = target.closest(TEXT_CONTROL);
  if (control) {
    const inside = control.querySelectorAll("*");
    const text = cutShortText(inside.length > CONTROL_SUBTREE_LIMIT ? [control] : [control, ...inside], control);
    return text ? { anchor: control, text } : null;
  }
  for (let node: Element | null = target, depth = 0; node && depth <= ANCESTORS_CHECKED; node = node.parentElement, depth += 1) {
    const text = cutShortText([node], node);
    if (text) return { anchor: node, text };
  }
  return null;
}

function preferredPlacement(anchor: Element): Placement {
  const value = anchor.getAttribute("data-tip-pos");
  return value === "bottom" || value === "left" || value === "right" ? value : "top";
}

/** Where the bubble goes for one placement, before clamping to the viewport. */
function placeAt(placement: Placement, anchor: DOMRect, bubble: DOMRect): { left: number; top: number } {
  const centredLeft = anchor.left + (anchor.width - bubble.width) / 2;
  const centredTop = anchor.top + (anchor.height - bubble.height) / 2;
  switch (placement) {
    case "bottom": return { left: centredLeft, top: anchor.bottom + GAP };
    case "left": return { left: anchor.left - bubble.width - GAP, top: centredTop };
    case "right": return { left: anchor.right + GAP, top: centredTop };
    default: return { left: centredLeft, top: anchor.top - bubble.height - GAP };
  }
}

/**
 * Whether a side has room, judged on its own axis only: the bubble can still
 * slide along the anchor's edge (the clamp below) without covering it.
 */
function fits(placement: Placement, position: { left: number; top: number }, bubble: DOMRect): boolean {
  if (placement === "top" || placement === "bottom") {
    return position.top >= VIEWPORT_MARGIN && position.top + bubble.height <= window.innerHeight - VIEWPORT_MARGIN;
  }
  return position.left >= VIEWPORT_MARGIN && position.left + bubble.width <= window.innerWidth - VIEWPORT_MARGIN;
}

/** Prefer the requested side, then its opposite, then the other axis; always stay on screen. */
export function resolveTipPosition(preferred: Placement, anchor: DOMRect, bubble: DOMRect): { left: number; top: number } {
  const crossAxis: Placement[] = preferred === "top" || preferred === "bottom" ? ["right", "left"] : ["bottom", "top"];
  const placements = [preferred, OPPOSITE[preferred], ...crossAxis];
  const candidates = placements.map((placement) => placeAt(placement, anchor, bubble));
  const position = candidates.find((candidate, index) => fits(placements[index], candidate, bubble)) ?? candidates[0];
  const clamp = (value: number, size: number, limit: number) =>
    Math.max(VIEWPORT_MARGIN, Math.min(value, limit - size - VIEWPORT_MARGIN));
  return {
    left: clamp(position.left, bubble.width, window.innerWidth),
    top: clamp(position.top, bubble.height, window.innerHeight),
  };
}

/**
 * Renders the tooltip for any element carrying `data-tip`, in a fixed layer on
 * top of the page, and the full text of any ellipsized text that has no title.
 * Scrolling panels cannot clip it, and it flips or shifts to stay inside the
 * window. The tip is decorative: triggers keep their own accessible names and
 * descriptions.
 */
export function TooltipLayer() {
  const [tip, setTip] = useState<ActiveTip | null>(null);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const bubbleRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let timer: number | undefined;
    let hideTimer: number | undefined;
    let pending: Element | null = null;
    const hide = () => {
      window.clearTimeout(timer);
      window.clearTimeout(hideTimer);
      pending = null;
      setTip(null);
    };
    const inBubble = (node: EventTarget | null) => node instanceof Node && Boolean(bubbleRef.current?.contains(node));
    // The pointer may cross the gap between the control and its tip, so the
    // tip can be hovered and read (WCAG 1.4.13); leaving both hides it.
    const hideSoon = () => {
      window.clearTimeout(hideTimer);
      hideTimer = window.setTimeout(hide, HOVER_GRACE_MS);
    };
    const keep = () => window.clearTimeout(hideTimer);
    // An explicit data-tip, preceded by any text it cuts short; otherwise text
    // cut short by an ellipsis explains itself, so every truncated value can
    // be read in full.
    const resolve = (target: EventTarget | null): { anchor: Element; text: string } | null => {
      const anchor = tipAnchor(target);
      const tipText = anchor?.getAttribute("data-tip")?.trim();
      if (!anchor) return truncatedTextAnchor(target);
      if (!tipText) return null;
      const cut = truncatedTextAnchor(anchor);
      return { anchor, text: cut && cut.anchor === anchor ? `${cut.text} · ${tipText}` : tipText };
    };
    // Keyboard focus scrolls its control into view after the tip is asked
    // for, and scrolling a hovered value into view lands its scroll event
    // after the pointer arrives; both must move the tip, not dismiss it. A tip
    // whose anchor scrolled out from under the pointer is dropped.
    const onScroll = () => {
      const anchor = pending;
      if (anchor && (anchor.contains(document.activeElement) || anchor.matches(":hover"))) {
        const box = anchor.getBoundingClientRect();
        if (box.bottom > 0 && box.top < window.innerHeight && box.right > 0 && box.left < window.innerWidth) {
          setTip((current) => (current ? { ...current } : current));
          return;
        }
      }
      hide();
    };
    const show = ({ anchor, text }: { anchor: Element; text: string }, delay: number, viaFocus = false) => {
      window.clearTimeout(timer);
      pending = anchor;
      timer = window.setTimeout(() => {
        if (pending !== anchor || !anchor.isConnected) return;
        setTip({ anchor, text, preferred: preferredPlacement(anchor), viaFocus });
      }, delay);
    };
    const onPointerOver = (event: PointerEvent) => {
      if (inBubble(event.target) || (pending && event.target instanceof Node && pending.contains(event.target))) {
        keep();
        return;
      }
      const found = resolve(event.target);
      // Moving onto another control drops the old tip at once; only the gap
      // between a control and its own tip gets the grace period.
      if (found || (event.target instanceof Element && event.target.closest(TEXT_CONTROL))) hide();
      if (found) show(found, HOVER_DELAY_MS);
    };
    const onPointerOut = (event: PointerEvent) => {
      const fromTip = inBubble(event.target) || (pending && event.target instanceof Node && pending.contains(event.target));
      if (!fromTip) return;
      const into = event.relatedTarget;
      if (inBubble(into) || (pending && into instanceof Node && pending.contains(into))) return;
      hideSoon();
    };
    const onFocusIn = (event: FocusEvent) => {
      if (!(event.target instanceof Element) || !event.target.matches(":focus-visible")) return;
      const found = resolve(event.target);
      if (found) show(found, 0, true);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") hide();
    };
    document.addEventListener("pointerover", onPointerOver);
    document.addEventListener("pointerout", onPointerOut);
    document.addEventListener("focusin", onFocusIn);
    document.addEventListener("focusout", hide);
    document.addEventListener("pointerdown", hide);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", hide);
    window.addEventListener("blur", hide);
    return () => {
      window.clearTimeout(timer);
      window.clearTimeout(hideTimer);
      document.removeEventListener("pointerover", onPointerOver);
      document.removeEventListener("pointerout", onPointerOut);
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("focusout", hide);
      document.removeEventListener("pointerdown", hide);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", hide);
      window.removeEventListener("blur", hide);
    };
  }, []);

  useLayoutEffect(() => {
    if (!tip || !bubbleRef.current) {
      setPosition(null);
      return;
    }
    if (!tip.anchor.isConnected) {
      setTip(null);
      return;
    }
    setPosition(resolveTipPosition(
      tip.preferred,
      tip.anchor.getBoundingClientRect(),
      bubbleRef.current.getBoundingClientRect(),
    ));
  }, [tip]);

  // A tip must not outlive its control: when the anchor is removed or hidden
  // (a job's rail button unmounting as the job ends), no pointer or focus
  // event fires for it, so watch the document instead.
  useEffect(() => {
    if (!tip) return undefined;
    const check = () => {
      const shown = typeof tip.anchor.checkVisibility === "function" ? tip.anchor.checkVisibility() : true;
      // A focused control removed from inside its (surviving) tip anchor fires
      // no focusout either.
      const focusGone = tip.viaFocus && !tip.anchor.contains(document.activeElement) && !tip.anchor.matches(":hover");
      if (!tip.anchor.isConnected || !shown || focusGone) setTip(null);
    };
    const observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class", "style"] });
    return () => observer.disconnect();
  }, [tip]);

  if (!tip) return null;
  return createPortal(
    <div
      ref={bubbleRef}
      className={`tooltip-bubble${position ? " is-placed" : ""}`}
      aria-hidden="true"
      style={position ? { left: position.left, top: position.top } : undefined}
    >
      {tip.text}
    </div>,
    document.body,
  );
}

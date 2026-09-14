import { type CSSProperties, type RefObject, useLayoutEffect, useState } from "react";

const MENU_GAP = 4;
const MENU_MAX_HEIGHT = 220;

/** Keep absolute menus inside every clipping scrollport, without moving focus. */
export function useTermSelectPlacement(open: boolean, rootRef: RefObject<HTMLDivElement>): CSSProperties {
  const [placement, setPlacement] = useState({ above: false, maxHeight: MENU_MAX_HEIGHT });

  useLayoutEffect(() => {
    const root = rootRef.current;
    if (!open || !root) return;

    const update = () => {
      const rect = root.getBoundingClientRect();
      let top = MENU_GAP;
      let bottom = window.innerHeight - MENU_GAP;
      for (let ancestor = root.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const overflow = getComputedStyle(ancestor).overflowY;
        if (!/^(auto|scroll|hidden|clip|overlay)$/.test(overflow)) continue;
        const bounds = ancestor.getBoundingClientRect();
        // clientHeight excludes borders and horizontal scrollbars.
        const innerTop = bounds.top + ancestor.clientTop;
        top = Math.max(top, innerTop);
        bottom = Math.min(bottom, innerTop + ancestor.clientHeight);
      }
      const below = Math.max(0, bottom - rect.bottom - MENU_GAP);
      const aboveSpace = Math.max(0, rect.top - top - MENU_GAP);
      const above = below < MENU_MAX_HEIGHT && aboveSpace > below;
      const maxHeight = Math.min(MENU_MAX_HEIGHT, above ? aboveSpace : below);
      setPlacement((current) => current.above === above && current.maxHeight === maxHeight
        ? current : { above, maxHeight });
    };

    update();
    // Scroll does not bubble; capture catches nested scrollport movement.
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [open, rootRef]);

  return {
    top: placement.above ? "auto" : `calc(100% + ${MENU_GAP}px)`,
    bottom: placement.above ? `calc(100% + ${MENU_GAP}px)` : "auto",
    maxHeight: placement.maxHeight,
  };
}

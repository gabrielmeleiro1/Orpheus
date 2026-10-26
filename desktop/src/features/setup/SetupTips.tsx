import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { Icon } from "../../ui/icons";

export function InfoTip({ children, label }: { children: ReactNode; label: string }) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number>();
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);

  const keepOpen = () => {
    window.clearTimeout(closeTimer.current);
    setOpen(true);
  };
  const closeSoon = () => {
    window.clearTimeout(closeTimer.current);
    // Allow the pointer to cross the gap between the icon and its panel.
    closeTimer.current = window.setTimeout(() => setOpen(false), 200);
  };

  useLayoutEffect(() => () => window.clearTimeout(closeTimer.current), []);

  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      if (!trigger.current || !bubble.current) return;
      const anchor = trigger.current.getBoundingClientRect();
      const panel = bubble.current.getBoundingClientRect();
      const margin = 16;
      const below = anchor.bottom + 8;
      const above = anchor.top - panel.height - 8;
      setPosition({
        left: Math.max(margin, Math.min(anchor.left - 12, window.innerWidth - panel.width - margin)),
        top: Math.max(margin, Math.min(
          below + panel.height <= window.innerHeight - margin ? below : above,
          window.innerHeight - panel.height - margin,
        )),
      });
    };
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node
        && !trigger.current?.contains(event.target)
        && !bubble.current?.contains(event.target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        window.clearTimeout(closeTimer.current);
        setOpen(false);
      }
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, [open, children]);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        className="btn btn-ghost btn-icon btn-info setup-tip"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-describedby={open ? id : undefined}
        onMouseEnter={keepOpen}
        onMouseLeave={closeSoon}
        onFocus={keepOpen}
        onBlur={closeSoon}
        onClickCapture={(event) => {
          event.preventDefault();
          event.stopPropagation();
          keepOpen();
        }}
      >
        <Icon name="info" />
      </button>
      {open && createPortal(
        <div
          ref={bubble}
          id={id}
          className="setup-tip-bubble"
          role="tooltip"
          onMouseEnter={keepOpen}
          onMouseLeave={closeSoon}
          style={{ ...position, visibility: position ? "visible" : "hidden" }}
        >{children}</div>,
        document.body,
      )}
    </>
  );
}

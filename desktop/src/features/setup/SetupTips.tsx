import { useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { groupEstimate, MODEL_GROUP_DETAILS } from "./setupModel";

export function InfoTip({ children, label }: { children: ReactNode; label: string }) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);

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
        setOpen(false);
        trigger.current?.focus();
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
        className="setup-tip"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-describedby={open ? id : undefined}
        onClickCapture={(event) => {
          event.preventDefault();
          event.stopPropagation();
          setPosition(null);
          setOpen(!open);
        }}
      >
        <span className="setup-tip-icon" aria-hidden="true">i</span>
      </button>
      {open && createPortal(
        <div
          ref={bubble}
          id={id}
          className="setup-tip-bubble"
          role="tooltip"
          style={{ ...position, visibility: position ? "visible" : "hidden" }}
        >{children}</div>,
        document.body,
      )}
    </>
  );
}

export function ModelListTip({ modelGroups, label }: { modelGroups: string[]; label: string }) {
  const rows = modelGroups
    .map((groupId) => ({ groupId, details: MODEL_GROUP_DETAILS[groupId] }))
    .filter((row) => Boolean(row.details));
  if (rows.length === 0) return null;
  return (
    <InfoTip label={label}>
      <ul className="setup-tip-models">
        {rows.map(({ groupId, details }) => (
          <li key={groupId}>
            <strong>{details!.label}</strong>
            <span>{details!.description}</span>
            <code>{details!.model}</code>
            <span>{groupEstimate(groupId)}</span>
          </li>
        ))}
      </ul>
    </InfoTip>
  );
}

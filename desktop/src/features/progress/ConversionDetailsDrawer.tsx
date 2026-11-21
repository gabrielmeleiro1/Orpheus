import { useEffect, useRef, useState } from "react";

import type { ConversionJobController } from "../conversion/useConversionJob";
import { LogPanel } from "../logs/LogPanel";
import { Icon } from "../../ui/icons";
import { ProgressPanel } from "./ProgressPanel";
import { parallelLimitNote } from "./progressTerminalPresentation";
import "./details-drawer.css";

interface ConversionDetailsDrawerProps {
  open: boolean;
  conversionJob: ConversionJobController;
  onClose: () => void;
}

/** True for controls inside a closed <details>, other than its own summary. */
function isFoldedAway(element: HTMLElement): boolean {
  const folded = element.closest("details:not([open])");
  return Boolean(folded) && !(element.tagName === "SUMMARY" && element.parentElement === folded);
}

/** Stages, performance and the event log for the current conversion, over whatever page is open. */
export function ConversionDetailsDrawer({ open, conversionJob, onClose }: ConversionDetailsDrawerProps) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  // The log is noisy while things go well; a failure opens it straight away.
  const [logOpen, setLogOpen] = useState(false);
  const failed = conversionJob.uiState === "failed";
  useEffect(() => {
    if (open && failed) setLogOpen(true);
  }, [failed, open]);

  useEffect(() => {
    if (!open) return;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const drawer = drawerRef.current;
    const parent = drawer?.parentElement;
    const madeInert = new Set<HTMLElement>();
    const concealBackground = () => {
      for (const sibling of parent?.children ?? []) {
        if (sibling instanceof HTMLElement && sibling !== drawer
          && !sibling.classList.contains("details-drawer-backdrop") && !sibling.hasAttribute("inert")) {
          sibling.setAttribute("inert", "");
          madeInert.add(sibling);
        }
      }
    };
    concealBackground();
    // Conversion completion can insert a notice while the drawer is open.
    const observer = new MutationObserver(concealBackground);
    if (parent) observer.observe(parent, { childList: true });
    closeRef.current?.focus();
    return () => {
      observer.disconnect();
      for (const sibling of madeInert) sibling.removeAttribute("inert");
      const opener = returnFocusRef.current;
      const target = opener?.isConnected && opener !== document.body && !opener.closest("[hidden], [inert]")
        ? opener : document.getElementById("workspace-heading");
      target?.focus({ preventScroll: true });
      returnFocusRef.current = null;
    };
  }, [open]);

  if (!open) return null;

  return (
    <>
      <div className="details-drawer-backdrop" aria-hidden="true" onClick={onClose} />
      <aside
        ref={drawerRef}
        id="conversion-details"
        className="details-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="Conversion details"
        onKeyDown={(event) => {
          if (event.defaultPrevented) return;
          if (event.key === "Tab") {
            const controls = [...event.currentTarget.querySelectorAll<HTMLElement>(
              "button, input, select, textarea, a[href], summary, [tabindex]",
            )].filter((element) => (element.tabIndex >= 0 || element.tagName === "SUMMARY") && !element.matches(":disabled")
              && !element.closest("[hidden], [inert]") && !isFoldedAway(element));
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) {
              event.preventDefault();
              last?.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
              event.preventDefault();
              first?.focus();
            }
          }
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            onClose();
          }
        }}
      >
        <header className="details-drawer-top">
          <span className="details-drawer-title">Conversion details</span>
          <button
            ref={closeRef}
            type="button"
            className="btn btn-ghost btn-sm btn-icon"
            aria-label="Close conversion details"
            data-tip="Close (Esc)"
            data-tip-pos="left"
            onClick={onClose}
          >
            <Icon name="x" />
          </button>
        </header>
        <div className="details-drawer-body">
          <ProgressPanel
            uiState={conversionJob.uiState}
            statusText={conversionJob.statusText}
            progressPct={conversionJob.progressPct}
            etaText={conversionJob.etaText}
            stages={conversionJob.stages}
            currentStage={conversionJob.currentStage}
            performance={conversionJob.performanceSnapshot}
            activeJobId={conversionJob.activeJobId}
            isCancelling={conversionJob.isCancelling}
            cancellationError={conversionJob.cancellationError}
            parallelNote={parallelLimitNote(conversionJob.logs, conversionJob.performanceSnapshot)}
            onCancel={conversionJob.cancelActiveConversion}
          />
          <section className="activity-log logs-panel" aria-labelledby="conversion-event-log-heading">
            <details className="activity-log-disclosure disclosure" open={logOpen} onToggle={(event) => setLogOpen(event.currentTarget.open)}>
              <summary>
                <h2 id="conversion-event-log-heading" className="eyebrow activity-log-title">Event log</h2>
                <span className="activity-log-count tabular-nums">{conversionJob.logs.length}</span>
              </summary>
              <LogPanel logs={conversionJob.logs} />
            </details>
          </section>
        </div>
      </aside>
    </>
  );
}

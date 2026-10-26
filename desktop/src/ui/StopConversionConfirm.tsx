import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

/**
 * Cancel stops a book mid-conversion, so it asks first. The question opens in
 * place of the trigger; Escape or "Keep converting" puts the trigger back and
 * returns focus to it. The question closes on its own when the job it was
 * about ends or changes, so it never confirms a stop for a different book.
 */
export function useStopConfirm(jobId: string | null) {
  const [armedFor, setArmedFor] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef(false);
  const armed = armedFor !== null && armedFor === jobId;

  useEffect(() => {
    if (armedFor !== null && armedFor !== jobId) setArmedFor(null);
  }, [armedFor, jobId]);

  useLayoutEffect(() => {
    if (!armed && restoreFocusRef.current) {
      restoreFocusRef.current = false;
      triggerRef.current?.focus();
    }
  }, [armed]);

  const arm = useCallback(() => {
    if (jobId) setArmedFor(jobId);
  }, [jobId]);
  const keep = useCallback(() => {
    restoreFocusRef.current = true;
    setArmedFor(null);
  }, []);
  const confirmed = useCallback(() => setArmedFor(null), []);

  return { armed, arm, keep, confirmed, triggerRef };
}

interface StopConversionConfirmProps {
  id: string;
  className?: string;
  onKeep: () => void;
  onStop: () => void;
}

export function StopConversionConfirm({ id, className, onKeep, onStop }: StopConversionConfirmProps) {
  return (
    <div
      id={id}
      className={`inline-confirm stop-confirm${className ? ` ${className}` : ""}`}
      role="group"
      aria-label="Confirm stopping the conversion"
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          event.stopPropagation();
          onKeep();
        }
      }}
    >
      <p className="stop-confirm-copy">Stop this conversion? Finished chapters are kept.</p>
      <div className="stop-confirm-buttons">
        <button id={`${id}-keep`} className="btn btn-secondary btn-sm" type="button" autoFocus onClick={onKeep}>
          Keep converting
        </button>
        <button id={`${id}-stop`} className="btn btn-danger btn-sm" type="button" onClick={onStop}>
          Stop conversion
        </button>
      </div>
    </div>
  );
}

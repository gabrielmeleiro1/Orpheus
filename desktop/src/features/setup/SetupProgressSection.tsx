import type { RefObject } from "react";

import { ProgressBar } from "../../ui/ProgressBar";
import { AsciiLoader } from "../../ui/ascii/AsciiLoader";
import type { SetupProgressState } from "./setupModel";

const STAGES = [
  { id: "packages", label: "Runtime packages" },
  { id: "models", label: "Model downloads" },
  { id: "verify", label: "Compatibility check" },
];

function StageState({ id, label, progress }: { id: string; label: string; progress: SetupProgressState }) {
  const complete = progress.completedStages.includes(id);
  const active = progress.lifecycle === "running" && progress.stage === id;
  return (
    <li className={`setup-stage${complete ? " is-complete" : active ? " is-active" : ""}`}>
      <span className="setup-stage-mark" aria-hidden="true">{complete ? "✓" : active ? <AsciiLoader active variant="ring" /> : "·"}</span>
      <span>{label}</span>
    </li>
  );
}

export function SetupProgressSection({
  progress,
  progressMessage,
  progressValue,
  headingRef,
}: {
  progress: SetupProgressState;
  progressMessage: string;
  progressValue: number | undefined;
  headingRef: RefObject<HTMLDivElement>;
}) {
  const statusLabel = progress.lifecycle === "failed" ? "Failed" : progress.restartRequired ? "Restart required" : progress.lifecycle === "complete" ? "Ready" : progress.lifecycle === "running" ? "Running" : "Needs setup";
  const statusTone = progress.lifecycle === "failed" ? "is-danger" : progress.restartRequired ? "is-warning" : progress.lifecycle === "complete" ? "is-success" : "is-neutral";
  return (
    <section className="setup-progress" aria-labelledby="setup-progress-heading">
      <div className="setup-progress-head">
        <div className="setup-subheading" id="setup-progress-heading" ref={headingRef} tabIndex={-1}>Installation</div>
        <span className={`setup-state ${statusTone}`}>{statusLabel}</span>
      </div>
      <p className="setup-progress-message" aria-live="polite">{progressMessage}</p>
      <ProgressBar
        pct={progressValue}
        tone={progress.lifecycle === "complete" ? "done" : "active"}
        label={progressValue === undefined ? "Setup in progress" : `Setup ${Math.round(progressValue)} percent complete`}
      />
      <ol className="setup-stage-list">{STAGES.map(({ id, label }) => <StageState key={id} id={id} label={label} progress={progress} />)}</ol>
    </section>
  );
}

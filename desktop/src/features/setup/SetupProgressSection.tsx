import type { RefObject } from "react";

import type { SetupProgressState } from "./setupModel";
import { ModelListTip } from "./SetupTips";

const STAGES = [
  { id: "packages", label: "Runtime packages" },
  { id: "models", label: "Model downloads" },
  { id: "verify", label: "Compatibility check" },
];

function StageState({ id, progress }: { id: string; progress: SetupProgressState }) {
  const complete = progress.completedStages.includes(id);
  const active = progress.lifecycle === "running" && progress.stage === id;
  return (
    <li className={`setup-stage${complete ? " is-complete" : active ? " is-active" : ""}`}>
      <span className="setup-stage-mark" aria-hidden="true">{complete ? "✓" : active ? "◌" : "·"}</span>
      <span>{STAGES.find((stage) => stage.id === id)?.label ?? id}</span>
    </li>
  );
}

export function SetupProgressSection({
  progress,
  progressMessage,
  progressValue,
  modelGroups,
  headingRef,
}: {
  progress: SetupProgressState;
  progressMessage: string;
  progressValue: number | undefined;
  modelGroups: string[];
  headingRef: RefObject<HTMLDivElement>;
}) {
  return (
    <section className="setup-progress card" aria-labelledby="setup-progress-heading">
      <div className="setup-progress-head">
        <div className="section-label" id="setup-progress-heading" ref={headingRef} tabIndex={-1}>
          Installation <ModelListTip modelGroups={modelGroups} label="Models being installed" />
        </div>
        <p className="setup-progress-message" aria-live="polite">{progressMessage}</p>
      </div>
      <progress value={progressValue} max={100} aria-label={progressValue === undefined ? "Setup in progress" : `Setup ${Math.round(progressValue)} percent complete`} />
      <ol className="setup-stage-list">{STAGES.map(({ id }) => <StageState key={id} id={id} progress={progress} />)}</ol>
    </section>
  );
}

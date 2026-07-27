import type { Dispatch, SetStateAction } from "react";

import type { RuntimeSetupProfile, RuntimeSetupStatusResponse } from "../../types/api";
import {
  groupEstimate,
  MODEL_GROUP_DETAILS,
  selectProfile,
  toggleModelGroup,
  type SetupProfileChoice,
  type SetupSelection,
} from "./setupModel";
import { SetupRiskNotice } from "./SetupRiskNotice";
import { InfoTip, ModelListTip } from "./SetupTips";

interface SetupPlanSectionProps {
  status: RuntimeSetupStatusResponse;
  selection: SetupSelection;
  setSelection: Dispatch<SetStateAction<SetupSelection>>;
  selectedProfile: RuntimeSetupProfile | null;
  selectedEstimate: string;
  choices: SetupProfileChoice[];
  busy: boolean;
  adopting: boolean;
  showAdvanced: boolean;
  setShowAdvanced: Dispatch<SetStateAction<boolean>>;
  showRiskBlock: boolean;
  riskProfile: RuntimeSetupProfile | null;
}

export function SetupPlanSection({
  status,
  selection,
  setSelection,
  selectedProfile,
  selectedEstimate,
  choices,
  busy,
  adopting,
  showAdvanced,
  setShowAdvanced,
  showRiskBlock,
  riskProfile,
}: SetupPlanSectionProps) {
  return (
    <>
      <div className="setup-plan">
        <div className="setup-plan-summary">
          <span className="setup-plan-title">
            <strong>{selection.profile === "base" ? "Library only" : selectedProfile?.label ?? selection.profile}</strong>
            {status.recommended_profile === selection.profile ? <span className="setup-chip">Recommended</span> : null}
            {selection.profile === "base"
              ? <InfoTip label="Library-only details"><span>Document conversion, playback, and cloud cleaning. No local models are installed — add them later in Settings.</span></InfoTip>
              : <ModelListTip modelGroups={selection.modelGroups} label="What will be installed" />}
          </span>
          <span className="setup-plan-detail">
            {selection.profile === "base"
              ? "No downloads"
              : `${selection.modelGroups.map((group) => MODEL_GROUP_DETAILS[group]?.label).filter(Boolean).join(" + ")} · ${selectedEstimate}`}
          </span>
        </div>
        <button className="btn btn-compact setup-customize-toggle" type="button" aria-expanded={showAdvanced} onClick={() => setShowAdvanced((open) => !open)}>
          {showAdvanced ? "Hide options" : "Customize"}
        </button>
      </div>

      {showAdvanced ? (
        <>
          <fieldset className="setup-section" disabled={busy}>
            <legend className="section-label">Runtime</legend>
            <div className="setup-profile-grid">
              {choices.map((profile) => (
                <label key={profile.id} className={`setup-profile-card${selection.profile === profile.id ? " is-selected" : ""}${profile.disabled ? " is-disabled" : ""}`}>
                  <input type="radio" name="runtime-profile" value={profile.id} checked={selection.profile === profile.id} disabled={profile.disabled} onChange={() => setSelection((current) => selectProfile(current, profile.id, status))} />
                  <span className="setup-card-radio" aria-hidden="true" />
                  <span className="setup-card-copy">
                    <span className="setup-card-head">
                      <strong>{profile.label}</strong>
                      {status.recommended_profile === profile.id && profile.compatible ? <span className="setup-chip">Recommended</span> : null}
                      <InfoTip label={`${profile.label} details`}><span>{profile.description}{profile.reason ? ` ${profile.reason}` : ""}</span></InfoTip>
                    </span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset className="setup-section setup-model-section" disabled={busy || !selectedProfile || selectedProfile.model_groups.length === 0}>
            <legend className="section-label">Models</legend>
            {selectedProfile?.model_groups.length ? (
              <div className="setup-model-list">
                {selectedProfile.model_groups.map((groupId) => {
                  const details = MODEL_GROUP_DETAILS[groupId];
                  if (!details) return null;
                  return (
                    <label className="setup-model-row" key={groupId}>
                      <input type="checkbox" checked={selection.modelGroups.includes(groupId)} onChange={() => setSelection((current) => toggleModelGroup(current, groupId, status))} />
                      <span className="setup-model-name">
                        <strong>{details.label}</strong>
                        <InfoTip label={`${details.label} details`}>
                          <span>{details.description}</span>
                          <code>{details.model}</code>
                        </InfoTip>
                      </span>
                      <span className="setup-estimate">{groupEstimate(groupId)}</span>
                    </label>
                  );
                })}
              </div>
            ) : <p className="setup-empty-models">No local models in library-only mode.</p>}
          </fieldset>
        </>
      ) : null}

      {showRiskBlock && riskProfile?.risk ? (
        <SetupRiskNotice
          summary={riskProfile.risk.summary}
          checked={selection.acceptPytorchRisk}
          disabled={busy || adopting}
          onChange={(event) => setSelection((current) => ({ ...current, acceptPytorchRisk: event.target.checked }))}
        />
      ) : null}
    </>
  );
}

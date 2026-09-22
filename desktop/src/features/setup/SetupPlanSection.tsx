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
import { SetupPrecisionChoice } from "./SetupPrecisionChoice";
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
  showModels?: boolean;
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
  showModels = true,
}: SetupPlanSectionProps) {
  const title = selection.profile === "base" ? "Library only" : selectedProfile?.label ?? selection.profile;
  const detailPrimary =
    selection.profile === "base"
      ? "No downloads"
      : `${selection.modelGroups.map((group) => MODEL_GROUP_DETAILS[group]?.label).filter(Boolean).join(" + ")} · ${selectedEstimate}`;

  return (
    <>
      <div className="setup-plan card">
        <div className="setup-plan-header">
          <span className="setup-plan-title">
            <strong>{title}</strong>
            {status.recommended_profile === selection.profile ? <span className="setup-chip">Recommended</span> : null}
            {selection.profile === "base"
              ? <InfoTip label="Library-only details"><span>Library, playback, and source inspection. Converting requires a local voice runtime — you can add one later from Settings.</span></InfoTip>
              : <ModelListTip modelGroups={selection.modelGroups} label="What will be installed" />}
          </span>
          <button className="btn setup-customize-toggle" type="button" aria-expanded={showAdvanced} onClick={() => setShowAdvanced((open) => !open)}>
            {showAdvanced ? "Hide runtimes" : "Change runtime"}
          </button>
        </div>
        <div className="setup-plan-body">
          <span className="setup-plan-detail">{detailPrimary}</span>
          {selection.modelGroups.length > 0 && selection.profile !== "base" ? (
            <span className="setup-plan-detail">{showModels ? "Models download from Hugging Face and run locally on your device." : "Choose the models to use below. Installed models are reused."}</span>
          ) : null}
        </div>
      </div>

      {showAdvanced ? (
        <>
          <fieldset className="setup-section" disabled={busy || adopting}>
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


        </>
      ) : null}

      {showModels && selectedProfile?.model_groups.length ? (
        <fieldset className="setup-section setup-model-section" disabled={busy || adopting}>
          <legend className="section-label">Choose what to install</legend>
          <p className="setup-model-explanation">Kokoro turns text into speech. The optional cleaner repairs text before narration. Choose one cleaner; other models can be installed or deleted later in Settings.</p>
          <div className="setup-model-list">
            {selectedProfile.model_groups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind === "tts").map((id) => {
              const installed = status.installed_runtime?.model_groups.some((group) => group.id === id && group.assets_ready);
              return <label className="setup-model-row" key={id}>
                <input type="checkbox" checked={selection.modelGroups.includes(id)} onChange={() => setSelection((current) => toggleModelGroup(current, id, status))} />
                <span className="setup-model-name"><strong>Kokoro voice model</strong><small>{MODEL_GROUP_DETAILS[id].model}</small></span>
                <span className="setup-estimate">{installed ? "Already downloaded" : groupEstimate(id)}</span>
              </label>;
            })}
            <label className="setup-model-row">
              <input type="radio" name="setup-cleaner" checked={!selection.modelGroups.some((id) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner")} onChange={() => setSelection((current) => ({ ...current, modelGroups: current.modelGroups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind !== "cleaner") }))} />
              <span className="setup-model-name"><strong>No local cleaner</strong><small>Skip the cleaner download. Basic text cleanup remains available.</small></span>
            </label>
            {selectedProfile.model_groups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner").map((id) => {
              const detail = MODEL_GROUP_DETAILS[id];
              const installed = status.installed_runtime?.model_groups.some((group) => group.id === id && group.assets_ready);
              return <label className="setup-model-row" key={id}>
                <input type="radio" name="setup-cleaner" checked={selection.modelGroups.includes(id)} onChange={() => setSelection((current) => toggleModelGroup(current, id, status))} />
                <span className="setup-model-name"><strong>{detail.label}</strong><small>{detail.description}</small><code>{detail.model}</code></span>
                <span className="setup-estimate">{installed ? "Already downloaded" : groupEstimate(id)}</span>
              </label>;
            })}
          </div>
        </fieldset>
      ) : null}
      <SetupPrecisionChoice selection={selection} disabled={busy || adopting} onChange={(value) => setSelection((current) => ({ ...current, cleanerCudaQuantization: value }))} />

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

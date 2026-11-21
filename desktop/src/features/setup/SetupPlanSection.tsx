import type { Dispatch, ReactNode, SetStateAction } from "react";

import type { RuntimeSetupProfile, RuntimeSetupStatusResponse } from "../../types/api";
import { RUNTIME_LABELS } from "../../format/runtime";
import { Icon } from "../../ui/icons";
import {
  MODEL_GROUP_DETAILS,
  selectProfile,
  toggleModelGroup,
  type SetupProfileChoice,
  type SetupSelection,
} from "./setupModel";
import { modelDownloadSize, shortIncompatibleReason } from "./setupPageHelpers";
import { SetupPrecisionChoice } from "./SetupPrecisionChoice";
import { InfoTip } from "./SetupTips";

const PRECISION_LABELS: Record<string, string> = { "4bit": "4-bit", "8bit": "8-bit", fp16: "FP16" };
const PROFILE_SUMMARIES: Record<string, string> = { base: "No downloads", mlx: "Apple Silicon", cuda: "NVIDIA GPU or CPU" };

interface SetupPlanSectionProps {
  status: RuntimeSetupStatusResponse;
  selection: SetupSelection;
  setSelection: Dispatch<SetStateAction<SetupSelection>>;
  selectedProfile: RuntimeSetupProfile | null;
  /** Approximate sizes of the selected models that still need downloading. */
  downloadEstimate: { download: string; disk: string } | null;
  choices: SetupProfileChoice[];
  busy: boolean;
  adopting: boolean;
  showAdvanced: boolean;
  setShowAdvanced: Dispatch<SetStateAction<boolean>>;
  configuredReady: boolean;
  installedReady: boolean;
  /** First run chooses models and precision behind the disclosure; Settings manages them in its own list. */
  variant: "onboarding" | "settings";
  /** A secondary action shown beside the disclosure toggle. */
  extraAction?: ReactNode;
}

export function SetupPlanSection({
  status,
  selection,
  setSelection,
  selectedProfile,
  downloadEstimate,
  choices,
  busy,
  adopting,
  showAdvanced,
  setShowAdvanced,
  configuredReady,
  installedReady,
  variant,
  extraAction,
}: SetupPlanSectionProps) {
  const onboarding = variant === "onboarding";
  const cpu = selectedProfile?.device === "cpu";
  const title = selection.profile === "base" ? "Library only" : `${selectedProfile?.label ?? selection.profile}${cpu ? " — running on CPU" : ""}`;
  const cleaner = selection.modelGroups.find((id) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner");
  // Settings lists the models and precision right below, so its summary only
  // adds what the list does not show: what still needs downloading.
  const detailParts = selection.profile === "base"
    ? ["Library and playback. Creating audiobooks needs a voice runtime."]
    : [
      onboarding ? selection.modelGroups.map((group) => MODEL_GROUP_DETAILS[group]?.label).filter(Boolean).join(" + ") : null,
      onboarding && cleaner && selection.profile === "cuda" ? PRECISION_LABELS[selection.cleanerCudaQuantization ?? "4bit"] : null,
      downloadEstimate ? `${downloadEstimate.download} download` : null,
    ].filter(Boolean);
  const detail = detailParts.join(" · ");
  const configuredRuntimeLabel = status.selected_profile === "base"
    ? "Library-only mode is ready"
    : `${status.profiles.find((profile) => profile.id === status.selected_profile)?.label ?? "Local"} runtime is ready`;
  const setCleaner = (groupId: string | null) => setSelection((current) => groupId
    ? toggleModelGroup(current, groupId, status)
    : { ...current, modelGroups: current.modelGroups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind !== "cleaner") });

  return (
    <>
      <div className={`setup-plan${onboarding ? " is-boxed" : ""}`}>
        <div className="setup-plan-copy">
          <span className="setup-plan-title">
            <strong className="setup-plan-name">{title}</strong>
            {onboarding && status.recommended_profile === selection.profile ? <span className="setup-plan-tag">Recommended</span> : null}
            {configuredReady ? <span className="setup-state is-success" role="status"><span className="setup-visually-hidden">{configuredRuntimeLabel}</span><span aria-hidden="true">Ready</span></span> : null}
            {installedReady && status.installed_runtime?.profile === selection.profile && !configuredReady ? <span className="setup-state is-success" role="note"><span className="setup-visually-hidden">Installed {RUNTIME_LABELS[status.installed_runtime.profile]} runtime ready</span><span aria-hidden="true">Ready</span></span> : null}
            {cpu ? <InfoTip label="Running on CPU"><span>No NVIDIA GPU found. Narration runs on your processor, about 2–3× faster than realtime. Local cleaners need an NVIDIA GPU; Basic cleanup is used instead.</span></InfoTip> : null}
          </span>
          {detail ? (
            <span className="setup-plan-detail" {...(downloadEstimate ? { "data-tip": `About ${downloadEstimate.download} to download, ${downloadEstimate.disk} on disk` } : {})}>{detail}</span>
          ) : null}
        </div>
        <div className="setup-plan-actions">
          {extraAction}
          <button className="btn btn-ghost btn-sm btn-flush-end setup-customize-toggle" type="button" aria-expanded={showAdvanced} aria-controls="setup-options" onClick={() => setShowAdvanced((open) => !open)}>
            {onboarding ? "Customize" : "Change runtime"}
            <Icon name="chevron-right" />
          </button>
        </div>
      </div>

      {showAdvanced ? (
        <div id="setup-options" className="setup-options">
          <fieldset className="setup-section" disabled={busy || adopting}>
            <legend className="setup-subheading">Runtime</legend>
            <div className="setup-profile-grid">
              {choices.map((profile) => {
                const installed = status.installed_runtime?.profile === profile.id && status.installed_runtime.runtime_available;
                const recommended = status.recommended_profile === profile.id && profile.compatible;
                const summary = profile.disabled
                  ? shortIncompatibleReason(profile.id, profile.reason)
                  : recommended ? "Recommended" : installed ? "Installed" : PROFILE_SUMMARIES[profile.id] ?? profile.description;
                return (
                  <label key={profile.id} className={`choice-card setup-profile-card${selection.profile === profile.id ? " is-selected" : ""}`} data-tip={(profile.disabled ? profile.reason : profile.description) ?? undefined}>
                    <input className="field-radio" type="radio" name="runtime-profile" value={profile.id} checked={selection.profile === profile.id} disabled={profile.disabled} onChange={() => setSelection((current) => selectProfile(current, profile.id, status))} />
                    <span className="setup-profile-copy">
                      <strong>{profile.label}</strong>
                      <span className="setup-card-description">{summary}</span>
                      {profile.disabled && profile.reason ? <span className="setup-visually-hidden">{profile.reason}</span> : null}
                    </span>
                  </label>
                );
              })}
            </div>
          </fieldset>

          {onboarding && selectedProfile?.model_groups.length ? (
            <fieldset className="setup-section setup-model-section" disabled={busy || adopting}>
              <legend className="setup-subheading">Models</legend>
              <div className="setup-model-list">
                <span className="setup-group-label" aria-hidden="true">Voice · required</span>
                {selectedProfile.model_groups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind === "tts").map((id) => {
                  const detail = MODEL_GROUP_DETAILS[id];
                  const selected = selection.modelGroups.includes(id);
                  const installed = status.installed_runtime?.model_groups.some((group) => group.id === id && group.assets_ready);
                  return <label className="setup-model-row" key={id}>
                    {/* Narration needs the voice model, so it stays checked; it is only offered when missing. */}
                    <input className="field-checkbox" type="checkbox" aria-label={`${detail.label} (required)`} checked={selected} disabled={selected} onChange={() => setSelection((current) => toggleModelGroup(current, id, status))} />
                    <span className="setup-model-name"><strong data-tip={detail.model}>{detail.label}</strong><small>{detail.description}</small></span>
                    <span className="setup-model-meta">
                      {installed ? <span className="setup-state is-success setup-model-status">Downloaded</span> : <span className="setup-model-size">{modelDownloadSize(id)}</span>}
                    </span>
                  </label>;
                })}
                <span className="setup-group-label" aria-hidden="true">Text cleaner · choose one</span>
                <label className="setup-model-row">
                  <input className="field-radio" type="radio" name="setup-cleaner" checked={!cleaner} onChange={() => setCleaner(null)} />
                  <span className="setup-model-name"><strong>No local cleaner</strong><small>Basic cleanup</small></span>
                  <span className="setup-model-meta" />
                </label>
                {selectedProfile.model_groups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner").map((id) => {
                  const detail = MODEL_GROUP_DETAILS[id];
                  const availability = selectedProfile.group_availability?.[id];
                  const installed = status.installed_runtime?.model_groups.some((group) => group.id === id && group.assets_ready);
                  const description = availability?.unavailable_reason ?? detail.description;
                  return <label className="setup-model-row" key={id}>
                    <input className="field-radio" type="radio" name="setup-cleaner" disabled={availability?.available === false} checked={selection.modelGroups.includes(id)} onChange={() => setCleaner(id)} />
                    <span className="setup-model-name"><strong data-tip={detail.model}>{detail.label}</strong><small>{description}</small></span>
                    <span className="setup-model-meta">
                      {installed ? <span className="setup-state is-success setup-model-status">Downloaded</span> : <span className="setup-model-size">{modelDownloadSize(id)}</span>}
                    </span>
                  </label>;
                })}
              </div>
            </fieldset>
          ) : null}
          {onboarding ? <SetupPrecisionChoice selection={selection} disabled={busy || adopting} onChange={(value) => setSelection((current) => ({ ...current, cleanerCudaQuantization: value }))} /> : null}
        </div>
      ) : null}
    </>
  );
}

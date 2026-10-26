import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { AsciiLoader } from "../../ui/ascii/AsciiLoader";

import type { ApiClient } from "../../api/client";
import { RUNTIME_LABELS } from "../../format/runtime";
import type { RuntimeModelInventory, RuntimeSetupProfile, RuntimeSetupStatusResponse } from "../../types/api";
import { MODEL_GROUP_DETAILS, toggleModelGroup, type SetupSelection } from "./setupModel";
import { formatFreedBytes, modelDownloadSize, safeError } from "./setupPageHelpers";

const PROFILE_LABELS: Record<string, string> = { ...RUNTIME_LABELS, base: "Library only", rocm: "ROCm" };
const KIND_LABELS = { tts: "Voice · required", cleaner: "Text cleaner · runtime model" } as const;

interface SetupModelManagerProps {
  apiClient: ApiClient | null;
  status: RuntimeSetupStatusResponse;
  selection: SetupSelection;
  setSelection: Dispatch<SetStateAction<SetupSelection>>;
  disabled: boolean;
  downloadingGroup: string | null;
  downloadMessage: string | null;
  onInstall: (profile: RuntimeSetupProfile["id"], groupId: string) => Promise<void>;
  onDeleted: () => Promise<void>;
  onBusyChange: (busy: boolean) => void;
}

export function SetupModelManager({ apiClient, status, selection, setSelection, disabled, downloadingGroup, downloadMessage, onInstall, onDeleted, onBusyChange }: SetupModelManagerProps) {
  const [inventory, setInventory] = useState<RuntimeModelInventory | null>(null);
  const [inventoryError, setInventoryError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const [error, setError] = useState<{ group: string; message: string } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const locked = useRef(false);
  const deleteTrigger = useRef<HTMLButtonElement | null>(null);
  const cancelDelete = () => {
    setConfirming(null);
    deleteTrigger.current?.focus();
  };
  const generation = useRef(0);

  useEffect(() => {
    generation.current += 1;
    locked.current = false;
    setDeleting(null);
    setConfirming(null);
    setInventory(null);
    return () => { generation.current += 1; };
  }, [apiClient]);

  useEffect(() => {
    if (!apiClient?.getRuntimeModelInventory) return;
    let active = true;
    void apiClient.getRuntimeModelInventory({ timeoutMs: 15_000 }).then((next) => {
      if (active) { setInventory(next); setInventoryError(null); }
    }).catch(() => {
      if (active) setInventoryError("Storage details could not be loaded.");
    });
    return () => { active = false; };
  }, [apiClient, status, refresh]);

  useEffect(() => { if (disabled) setConfirming(null); }, [disabled]);

  const remove = async (groupId: string) => {
    if (!apiClient || disabled || locked.current) return;
    locked.current = true;
    const current = generation.current;
    setDeleting(groupId);
    setConfirming(null);
    setError(null);
    setNotice(null);
    onBusyChange(true);
    try {
      const result = await apiClient.deleteRuntimeModel(groupId, { timeoutMs: 120_000 });
      if (current !== generation.current) return;
      if (result.residual_model_groups?.length) {
        setNotice(`${result.message} Freed ${formatFreedBytes(result.freed_bytes)}.`);
        await onDeleted();
        if (current === generation.current) setRefresh((value) => value + 1);
        return;
      }
      setInventory((previous) => previous ? { models: previous.models.map((model) => model.id === groupId ? { ...model, downloaded: false, size_bytes: 0 } : model) } : previous);
      setNotice(`${MODEL_GROUP_DETAILS[groupId].label} deleted. Freed ${formatFreedBytes(result.freed_bytes)}.`);
      await onDeleted();
      if (current === generation.current) setRefresh((value) => value + 1);
    } catch (reason: unknown) {
      if (current === generation.current) setError({ group: groupId, message: safeError(reason, "The model could not be deleted. Try again.") });
    } finally {
      if (current === generation.current) {
        locked.current = false;
        setDeleting(null);
        onBusyChange(false);
      }
    }
  };

  const models = status.profiles.flatMap((profile) => profile.model_groups.map((id) => ({ id, profile })))
    .filter(({ id, profile }) => MODEL_GROUP_DETAILS[id] && (profile.compatible || inventory?.models.some((model) => model.id === id && model.downloaded)));
  const totalBytes = inventory?.models.reduce((sum, model) => sum + model.size_bytes, 0);
  const choosingCleaner = selection.profile !== "base" && status.profiles.some((profile) => profile.id === selection.profile && profile.model_groups.some((id) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner"));
  const noCleaner = !selection.modelGroups.some((id) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner");

  const renderModel = ({ id, profile }: (typeof models)[number]) => {
    const details = MODEL_GROUP_DETAILS[id];
    const availability = profile.group_availability?.[id];
    const cached = inventory?.models.find((model) => model.id === id);
    const capability = status.installed_runtime?.profile === profile.id
      ? status.installed_runtime.model_groups.find((group) => group.id === id)
      : status.selected_profile === profile.id ? status.selected_model_groups.find((group) => group.id === id) : undefined;
    const downloaded = cached?.downloaded ?? capability?.assets_ready ?? false;
    const active = downloadingGroup === id;
    const pending = deleting === id;
    const blocked = disabled || deleting !== null;
    // The backend says when a model cannot be deleted now (for example while a conversion uses it).
    const deleteBlockedReason = cached?.deletable === false ? cached.delete_blocked?.message ?? "This model is in use." : null;
    const installed = Boolean(capability?.assets_ready && downloaded);
    const selected = selection.modelGroups.includes(id);
    const voice = details.kind === "tts";
    const stateLabel = pending ? "Deleting…" : active ? "Downloading…" : cached?.damaged ? "Damaged" : installed ? "Downloaded" : downloaded ? "Needs attention" : null;
    const stateTone = pending || active ? "is-neutral" : cached?.damaged ? "is-danger" : installed ? "is-success" : "is-warning";
    const size = downloaded && cached?.size_bytes ? formatFreedBytes(cached.size_bytes) : downloaded ? null : modelDownloadSize(id);
    const unavailableReason = (availability?.available === false || (!installed && !capability?.runtime_ready)) && !active
      ? availability?.unavailable_reason ?? (profile.compatible ? "The local runtime must be available before this model can be downloaded." : "This runtime is not supported on this device.")
      : null;
    return (
      <article className={`setup-managed-model${active ? " is-downloading" : ""}`} key={id} aria-label={details.label} aria-busy={pending || active}>
        <div className="setup-model-row setup-managed-model-main">
          <label className="setup-managed-model-choice">
            {selection.profile === profile.id ? (
              voice
                // Narration needs the voice model: it stays checked, and is only offered when missing.
                ? <input className="field-checkbox" type="checkbox" aria-label={`Use ${details.label} in this runtime (required)`} checked={selected} disabled={selected || blocked || availability?.available === false} onChange={() => setSelection((current) => toggleModelGroup(current, id, status))} />
                : <input className="field-radio" type="radio" name="managed-cleaner" aria-label={`Set up this runtime with ${details.label}`} aria-describedby="managed-cleaner-hint" checked={selected} disabled={blocked || availability?.available === false} onChange={() => setSelection((current) => toggleModelGroup(current, id, status))} />
            ) : <span className="setup-model-control-slot" aria-hidden="true" />}
            <span className="setup-model-name">
              <strong data-tip={`${details.model} · ${PROFILE_LABELS[profile.id] ?? profile.label}`}>{details.label}</strong>
              <small>{unavailableReason ?? details.description}</small>
            </span>
          </label>
          <span className="setup-model-size">{size}</span>
          <span className={`setup-state setup-model-status ${stateTone}`}>
            {pending || active ? <AsciiLoader active width={4} className="setup-loading-spinner" /> : null}
            {stateLabel}
          </span>
          <div className="setup-model-buttons">
            {!installed ? <button className="btn btn-ghost btn-sm btn-flush-end" type="button" disabled={blocked || !profile.compatible || !capability?.runtime_ready || !apiClient} aria-label={`Download ${details.label}`} onClick={() => { setNotice(null); void onInstall(profile.id, id); }}>{downloaded ? "Repair" : "Download"}</button> : null}
            {downloaded ? <button className="btn btn-ghost btn-sm btn-flush-end btn-quiet-danger setup-delete" type="button" disabled={blocked || !apiClient || deleteBlockedReason !== null} data-tip={deleteBlockedReason ?? undefined} aria-label={`Delete ${details.label}`} onClick={(event) => { deleteTrigger.current = event.currentTarget; setError(null); setConfirming(id); }}>Delete</button> : null}
          </div>
        </div>
        {active && downloadMessage ? <p className="setup-model-feedback" role="status">{downloadMessage}</p> : null}
        {confirming === id ? <div className="inline-confirm setup-model-confirm" role="group" aria-label={`Confirm deleting ${details.label}`} onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); cancelDelete(); } }}>
          <p>Delete {details.label} from this device? Your books and audio will stay.</p>
          <div className="setup-model-buttons"><button className="btn btn-secondary btn-sm" type="button" autoFocus onClick={cancelDelete}>Cancel</button><button className="btn btn-danger btn-sm" type="button" onClick={() => void remove(id)}>Delete model</button></div>
        </div> : null}
        {error?.group === id ? <p className="setup-model-feedback is-error" role="alert">{error.message}</p> : null}
      </article>
    );
  };

  return (
    <section className="setup-model-manager" aria-labelledby="managed-models-heading">
      <div className="setup-model-manager-heading">
        <h3 className="setup-subheading" id="managed-models-heading">Models</h3>
        {totalBytes !== undefined ? <span className="setup-storage-total">{formatFreedBytes(totalBytes)} on disk</span> : null}
      </div>
      {inventoryError ? <div className="setup-model-feedback" role="status">{inventoryError} <button className="btn btn-ghost btn-sm" type="button" disabled={disabled || deleting !== null} onClick={() => setRefresh((value) => value + 1)}>Retry</button></div> : null}
      {notice ? <p className="setup-model-feedback" role="status">{notice}</p> : null}
      <div className="setup-model-list">
        {(["tts", "cleaner"] as const).map((kind) => {
          const rows = models.filter(({ id }) => MODEL_GROUP_DETAILS[id].kind === kind);
          if (!rows.length) return null;
          return (
            <div className="setup-managed-models" key={kind}>
              <span className="setup-group-label" aria-hidden="true">{KIND_LABELS[kind]}</span>
              {kind === "cleaner" && choosingCleaner ? (
                <p className="setup-group-hint" id="managed-cleaner-hint">The model this runtime is set up with. Text cleaning in Settings chooses the cleaner your books use.</p>
              ) : null}
              {kind === "cleaner" && choosingCleaner ? (
                <div className="setup-model-row setup-no-cleaner">
                  <label className="setup-managed-model-choice">
                    <input className="field-radio" type="radio" name="managed-cleaner" aria-describedby="managed-cleaner-hint" checked={noCleaner} disabled={disabled || deleting !== null} onChange={() => setSelection((current) => ({ ...current, modelGroups: current.modelGroups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind !== "cleaner") }))} />
                    <span className="setup-model-name"><strong>No local cleaner</strong><small>Basic cleanup by default</small></span>
                  </label>
                </div>
              ) : null}
              {rows.map(renderModel)}
            </div>
          );
        })}
      </div>
    </section>
  );
}

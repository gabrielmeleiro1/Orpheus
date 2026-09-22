import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

import type { ApiClient } from "../../api/client";
import { Icon } from "../../ui/icons";
import type { RuntimeModelInventory, RuntimeSetupProfile, RuntimeSetupStatusResponse } from "../../types/api";
import { MODEL_GROUP_DETAILS, toggleModelGroup, type SetupSelection } from "./setupModel";
import { formatFreedBytes, safeError } from "./setupPageHelpers";

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
  const [removed, setRemoved] = useState<string[]>([]);
  const locked = useRef(false);
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
      setRemoved((previous) => [...new Set([...previous, groupId])]);
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

  return (
    <section className="setup-model-manager" aria-labelledby="managed-models-heading">
      <div className="setup-model-manager-heading">
        <div><h2 id="managed-models-heading">Models on this device</h2><p>Keep several models downloaded. Select one cleaner for your next conversion, then save your settings.</p></div>
        {totalBytes !== undefined ? <span className="setup-storage-total">{formatFreedBytes(totalBytes)} used</span> : null}
      </div>
      {inventoryError ? <div className="setup-model-feedback" role="status">{inventoryError} <button className="btn btn-ghost" disabled={disabled || deleting !== null} onClick={() => setRefresh((value) => value + 1)}>Retry storage check</button></div> : null}
      {notice ? <p className="setup-model-feedback" role="status">{notice}</p> : null}
      <div className="setup-managed-models">
        {models.map(({ id, profile }) => {
          const details = MODEL_GROUP_DETAILS[id];
          const cached = inventory?.models.find((model) => model.id === id);
          const capability = status.installed_runtime?.profile === profile.id
            ? status.installed_runtime.model_groups.find((group) => group.id === id)
            : status.selected_profile === profile.id ? status.selected_model_groups.find((group) => group.id === id) : undefined;
          const downloaded = cached?.downloaded ?? capability?.assets_ready ?? false;
          const active = downloadingGroup === id;
          const pending = deleting === id;
          const blocked = disabled || deleting !== null;
          const installed = Boolean(capability?.assets_ready && downloaded);
          const selected = selection.modelGroups.includes(id);
          const saved = status.selected_model_groups.some((group) => group.id === id);
          const label = pending ? "Deleting…" : active ? "Downloading…" : cached?.damaged ? "Damaged" : installed ? "Installed" : downloaded ? "Needs attention" : "Not installed";
          return (
            <article className={`setup-managed-model${active ? " is-downloading" : ""}`} key={id} aria-label={details.label} aria-busy={pending || active}>
              <div className="setup-managed-model-main">
                <div className="setup-model-kind" aria-hidden="true"><Icon name={details.kind === "tts" ? "convert" : "document"} size={22} /></div>
                <div className="setup-managed-model-copy">
                  <div className="setup-managed-model-title"><h3>{details.label}</h3><span className={`setup-model-badge${installed ? " is-installed" : ""}`}>{pending || active ? <span className="setup-loading-spinner" aria-hidden="true" /> : null}{label}</span>{saved ? <span className="setup-chip">Saved default</span> : selected ? <span className="setup-chip">Selected · save to apply</span> : null}</div>
                  <p>{details.description}</p>
                  <span className="setup-model-storage">{downloaded && cached ? `${formatFreedBytes(cached.size_bytes)} on disk` : `~${formatFreedBytes(details.downloadMB * 1_000_000)} download`} · {profile.id.toUpperCase()}</span>
                  <details className="setup-model-detail"><summary>Model details</summary><code>{details.model}</code></details>
                </div>
                <div className="setup-model-buttons">
                  {!installed ? <button className="btn btn-accent-soft" type="button" disabled={blocked || !profile.compatible || !capability?.runtime_ready || !apiClient} aria-label={`Install ${details.label}`} onClick={() => { setNotice(null); void onInstall(profile.id, id); }}>{active ? "Installing…" : downloaded ? "Repair download" : removed.includes(id) ? "Install again" : "Install"}</button> : null}
                  {downloaded ? <button className="btn btn-ghost setup-model-delete" type="button" disabled={blocked || !apiClient} aria-label={`Delete ${details.label}`} onClick={() => { setError(null); setConfirming(id); }}>{pending ? "Deleting…" : "Delete"}</button> : null}
                </div>
              </div>
              {selection.profile === profile.id ? <label className="setup-model-use"><input type={details.kind === "cleaner" ? "radio" : "checkbox"} name={details.kind === "cleaner" ? "managed-cleaner" : undefined} checked={selected} disabled={blocked} onChange={() => setSelection((current) => toggleModelGroup(current, id, status))} /><span>Use {details.label} in this runtime</span></label> : null}
              {!installed && !capability?.runtime_ready && !active ? <p className="setup-model-feedback">{profile.compatible ? "The local runtime must be available before this model can be installed." : "This runtime is not supported on this device."}</p> : null}
              {active && downloadMessage ? <p className="setup-model-feedback" role="status">{downloadMessage}</p> : null}
              {confirming === id ? <div className="setup-model-confirm" role="group" aria-label={`Confirm deleting ${details.label}`} onKeyDown={(event) => { if (event.key === "Escape") setConfirming(null); }}>
                <p>Delete {details.label} from this device? You can install it again later. Your books and audio will stay.</p>
                <div className="setup-model-buttons"><button className="btn" type="button" autoFocus onClick={() => setConfirming(null)}>Keep model</button><button className="btn btn-danger" type="button" onClick={() => void remove(id)}>Delete model</button></div>
              </div> : null}
              {error?.group === id ? <p className="setup-model-feedback is-error" role="alert">{error.message}</p> : null}
            </article>
          );
        })}
      </div>
      <label className="setup-model-use"><input type="radio" name="managed-cleaner" checked={!selection.modelGroups.some((id) => MODEL_GROUP_DETAILS[id]?.kind === "cleaner")} disabled={disabled || deleting !== null} onChange={() => setSelection((current) => ({ ...current, modelGroups: current.modelGroups.filter((id) => MODEL_GROUP_DETAILS[id]?.kind !== "cleaner") }))} /><span>No local cleaner</span></label>
      <p className="setup-model-footnote">Switching models keeps existing downloads. Install only downloads files; it does not start or select another cleaner. Delete removes that model from the shared Hugging Face cache. Books and audio are kept.</p>
    </section>
  );
}

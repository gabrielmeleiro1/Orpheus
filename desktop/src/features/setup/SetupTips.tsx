import type { ReactNode } from "react";

import { groupEstimate, MODEL_GROUP_DETAILS } from "./setupModel";

export function InfoTip({ children, label }: { children: ReactNode; label: string }) {
  return (
    <span className="setup-tip" tabIndex={0} aria-label={label}>
      <span className="setup-tip-icon" aria-hidden="true">i</span>
      <span className="setup-tip-bubble" role="tooltip">{children}</span>
    </span>
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

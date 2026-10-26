import type { ChangeEventHandler } from "react";

import { Icon } from "../../ui/icons";

export function SetupRiskNotice({
  summary,
  checked,
  disabled,
  onChange,
}: {
  summary: string;
  checked: boolean;
  disabled: boolean;
  onChange: ChangeEventHandler<HTMLInputElement>;
}) {
  return (
    <div className="setup-recovery setup-risk" role="note">
      <span className="setup-recovery-icon" aria-hidden="true">
        <Icon name="alert" />
      </span>
      <div className="setup-recovery-copy">
        <strong>PyTorch security notice</strong>
        <p>{summary}</p>
        <label className="choice">
          <input className="field-checkbox" type="checkbox" checked={checked} disabled={disabled} onChange={onChange} />
          <span>I understand and will use only trusted model files.</span>
        </label>
      </div>
    </div>
  );
}

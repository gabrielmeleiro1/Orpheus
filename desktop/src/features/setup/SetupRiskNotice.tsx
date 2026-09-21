import type { ChangeEventHandler } from "react";

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
    <div className="setup-risk" role="note">
      <strong>PyTorch security notice</strong>
      <p>{summary}</p>
      <label>
        <input type="checkbox" checked={checked} disabled={disabled} onChange={onChange} />
        <span>I understand and will use only trusted model files.</span>
      </label>
    </div>
  );
}

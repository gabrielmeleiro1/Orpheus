import type { ReactNode } from "react";

import { Icon } from "../../ui/icons";

interface SetupRecoveryNoticeProps {
  title: string;
  message: string;
  /** The raw error behind the message, shown small and secondary. */
  detail?: string | null;
  /** Recovery buttons, laid out as one row under the message. */
  children: ReactNode;
  environment?: boolean;
}

export function SetupRecoveryNotice({ title, message, detail, children, environment = false }: SetupRecoveryNoticeProps) {
  return (
    <div className={`setup-recovery${environment ? " setup-environment" : ""}`} role={environment ? "note" : "alert"}>
      <span className="setup-recovery-icon" aria-hidden="true">
        <Icon name="alert" />
      </span>
      <div className="setup-recovery-copy">
        <strong>{title}</strong>
        <p>{message}</p>
        {detail && detail !== message ? <span className="error-detail">{detail}</span> : null}
        <div className="setup-recovery-actions">{children}</div>
      </div>
    </div>
  );
}

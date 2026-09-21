import type { ReactNode } from "react";

import { Icon } from "../../ui/icons";

interface SetupRecoveryNoticeProps {
  title: string;
  message: string;
  children: ReactNode;
  environment?: boolean;
}

export function SetupRecoveryNotice({ title, message, children, environment = false }: SetupRecoveryNoticeProps) {
  return (
    <div className={`setup-recovery${environment ? " setup-environment" : ""}`} role={environment ? "note" : "alert"}>
      <div className="setup-recovery-copy">
        <span className="setup-recovery-icon" aria-hidden="true">
          <Icon name="alert" />
        </span>
        <div>
          <strong>{title}</strong>
          <p>{message}</p>
        </div>
      </div>
      {children}
    </div>
  );
}

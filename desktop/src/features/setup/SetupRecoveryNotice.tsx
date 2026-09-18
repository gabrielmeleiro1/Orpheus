import type { ReactNode } from "react";

interface SetupRecoveryNoticeProps {
  title: string;
  message: string;
  children: ReactNode;
  environment?: boolean;
}

export function SetupRecoveryNotice({ title, message, children, environment = false }: SetupRecoveryNoticeProps) {
  return (
    <div className={`setup-recovery${environment ? " setup-environment" : ""}`} role={environment ? "note" : "alert"}>
      <div>
        <strong>{title}</strong>
        <p>{message}</p>
      </div>
      {children}
    </div>
  );
}

import type { EventStreamLogEntry } from "../../types/api";
import "./logs.css";

interface LogPanelProps {
  logs: EventStreamLogEntry[];
}

function levelClass(level: string): string {
  const normalized = String(level || "").toLowerCase();
  if (normalized === "warn" || normalized === "warning") {
    return " log-warn";
  }
  if (normalized === "error") {
    return " log-error";
  }
  return "";
}

export function LogPanel({ logs }: LogPanelProps) {
  return (
    <>
      <div className="panel-header">
        <span>process log</span>
        <span className="panel-header-meta">{logs.length} {logs.length === 1 ? "entry" : "entries"}</span>
      </div>
      <div id="log-box" className="panel-section log-box" role="log" aria-live="polite" aria-relevant="additions">
        {logs.length === 0 ? (
          <div className="log-line">
            <span className="log-ts">[--:--:--]</span>
            <span> waiting for events...</span>
          </div>
        ) : (
          logs.map((entry) => (
            <div key={entry.id} className={`log-line${levelClass(entry.level)}`}>
              <span className="log-ts">{`[${entry.ts}]`}</span>
              <span>{` ${entry.msg}`}</span>
            </div>
          ))
        )}
      </div>
    </>
  );
}

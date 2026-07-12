import { useMemo, useState } from "react";

import type { EventStreamLogEntry } from "../../types/api";
import {
  filterVisibleLogs,
  formatLogsForClipboard,
  type LogCategoryFilter,
  type LogLevelFilter,
} from "./logPresentation";
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
  const [level, setLevel] = useState<LogLevelFilter>("all");
  const [category, setCategory] = useState<LogCategoryFilter>("all");
  const [copyStatus, setCopyStatus] = useState("");
  const visibleLogs = useMemo(
    () => filterVisibleLogs(logs, { level, category }),
    [category, level, logs],
  );

  const onCopyVisible = async () => {
    const text = formatLogsForClipboard(visibleLogs);
    if (!text) {
      setCopyStatus("Nothing to copy.");
      return;
    }
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard unavailable");
      }
      await navigator.clipboard.writeText(text);
      setCopyStatus(`Copied ${visibleLogs.length} ${visibleLogs.length === 1 ? "entry" : "entries"}.`);
    } catch {
      setCopyStatus("Copy failed — clipboard unavailable.");
    }
  };

  return (
    <>
      <div className="panel-header">
        <span>process log</span>
        <span className="panel-header-meta">
          {visibleLogs.length}/{logs.length} {logs.length === 1 ? "entry" : "entries"}
        </span>
      </div>
      <div className="log-toolbar" aria-label="Log filters">
        <label className="field-label" htmlFor="log-level-filter">
          level
        </label>
        <select
          id="log-level-filter"
          className="field-input log-filter"
          value={level}
          onChange={(event) => setLevel(event.target.value as LogLevelFilter)}
        >
          <option value="all">all</option>
          <option value="info">info</option>
          <option value="warn">warn</option>
          <option value="error">error</option>
        </select>
        <label className="field-label" htmlFor="log-category-filter">
          category
        </label>
        <select
          id="log-category-filter"
          className="field-input log-filter"
          value={category}
          onChange={(event) => setCategory(event.target.value as LogCategoryFilter)}
        >
          <option value="all">all</option>
          <option value="state">state</option>
          <option value="transport">transport</option>
          <option value="conversion">conversion</option>
          <option value="other">other</option>
        </select>
        <button className="btn btn-compact" type="button" onClick={() => void onCopyVisible()}>
          copy visible logs
        </button>
        <span className="field-hint" role="status">
          {copyStatus}
        </span>
      </div>
      <div id="log-box" className="panel-section log-box" role="log" aria-live="polite" aria-relevant="additions">
        {visibleLogs.length === 0 ? (
          <div className="log-line">
            <span className="log-ts">[--:--:--]</span>
            <span> {logs.length === 0 ? "waiting for events..." : "no entries match the current filters"}</span>
          </div>
        ) : (
          visibleLogs.map((entry) => (
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

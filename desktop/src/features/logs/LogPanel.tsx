import { useEffect, useMemo, useRef, useState } from "react";

import type { EventStreamLogEntry } from "../../types/api";
import { TermSelect } from "../conversion/TermSelect";
import {
  countLogLevels,
  filterVisibleLogs,
  formatLogsForClipboard,
  normalizeLogLevel,
  type LogCategoryFilter,
  type LogLevelFilter,
} from "./logPresentation";
import "./logs.css";

interface LogPanelProps {
  logs: EventStreamLogEntry[];
}

const LEVEL_SEGMENTS: { value: LogLevelFilter; label: string; tip: string }[] = [
  { value: "all", label: "All", tip: "Every log entry" },
  { value: "info", label: "Info", tip: "Routine activity" },
  { value: "warn", label: "Warn", tip: "Recovered problems worth a look" },
  { value: "error", label: "Error", tip: "Failures that need attention" },
];

const CATEGORY_OPTIONS = [
  { value: "all", label: "All sources" },
  { value: "state", label: "Job state" },
  { value: "transport", label: "Connection" },
  { value: "conversion", label: "Conversion" },
  { value: "other", label: "Other" },
];

function lineClass(level: string): string {
  const normalized = normalizeLogLevel(level);
  if (normalized === "warn") {
    return "log-line log-warn";
  }
  if (normalized === "error") {
    return "log-line log-error";
  }
  return "log-line";
}

export function LogPanel({ logs }: LogPanelProps) {
  const [level, setLevel] = useState<LogLevelFilter>("all");
  const [category, setCategory] = useState<LogCategoryFilter>("all");
  const [query, setQuery] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const [following, setFollowing] = useState(true);
  const scrollBoxRef = useRef<HTMLDivElement | null>(null);

  const visibleLogs = useMemo(
    () => filterVisibleLogs(logs, { level, category, query }),
    [category, level, logs, query],
  );
  const levelCounts = useMemo(() => countLogLevels(logs), [logs]);
  // Entries arrive newest-first, so "following" means staying pinned to the top.
  const newestEntryId = visibleLogs.length > 0 ? visibleLogs[0].id : null;

  useEffect(() => {
    if (!following) {
      return;
    }
    const box = scrollBoxRef.current;
    if (box) {
      box.scrollTop = 0;
    }
  }, [following, newestEntryId, level, category, query]);

  useEffect(() => {
    if (!copyStatus) {
      return undefined;
    }
    const id = window.setTimeout(() => setCopyStatus(""), 2500);
    return () => {
      window.clearTimeout(id);
    };
  }, [copyStatus]);

  const onScroll = () => {
    const box = scrollBoxRef.current;
    if (!box) {
      return;
    }
    const atTop = box.scrollTop < 24;
    if (atTop !== following) {
      setFollowing(atTop);
    }
  };

  const jumpToLatest = () => {
    const box = scrollBoxRef.current;
    if (box) {
      box.scrollTop = 0;
    }
    setFollowing(true);
  };

  const onCopyVisible = async () => {
    const text = formatLogsForClipboard(visibleLogs);
    if (!text) {
      setCopyStatus("Nothing to copy");
      return;
    }
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard unavailable");
      }
      await navigator.clipboard.writeText(text);
      setCopyStatus(`Copied ${visibleLogs.length} ${visibleLogs.length === 1 ? "entry" : "entries"}`);
    } catch {
      setCopyStatus("Clipboard unavailable");
    }
  };

  return (
    <>
      <div className="panel-header">
        <span>Process log</span>
        <span className="panel-header-meta">
          {visibleLogs.length === logs.length
            ? `${logs.length} ${logs.length === 1 ? "entry" : "entries"}`
            : `${visibleLogs.length} of ${logs.length} shown`}
        </span>
      </div>

      <div className="log-toolbar" aria-label="Log filters">
        <div className="seg-control log-level-control" role="group" aria-label="Filter by level">
          {LEVEL_SEGMENTS.map(({ value, label, tip }) => {
            const count = levelCounts[value];
            return (
              <button
                key={value}
                id={`log-level-${value}`}
                type="button"
                className={`seg-control-btn${level === value ? " is-active" : ""}`}
                data-tip={tip}
                data-tip-pos="bottom"
                onClick={() => setLevel(value)}
              >
                {label}
                {value !== "all" && count > 0 ? <span className="log-level-count">{count}</span> : null}
              </button>
            );
          })}
        </div>

        <TermSelect
          id="log-category-filter"
          label="Filter by source"
          value={category}
          options={CATEGORY_OPTIONS}
          placeholder="All sources"
          onChange={(value) => setCategory(value as LogCategoryFilter)}
        />

        <input
          id="log-search"
          className="field-input log-search"
          type="search"
          placeholder="Search…"
          aria-label="Search log messages"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />

        <div className="log-toolbar-spacer" />

        <span className="field-hint log-copy-status" role="status">
          {copyStatus}
        </span>
        <button
          id="log-copy-btn"
          className="btn btn-ghost btn-compact"
          type="button"
          data-tip="Copy the entries currently shown, with timestamps"
          data-tip-pos="bottom"
          onClick={() => void onCopyVisible()}
        >
          Copy
        </button>
      </div>

      <div className="log-shell">
        <div
          id="log-box"
          ref={scrollBoxRef}
          className="panel-section log-box"
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          onScroll={onScroll}
        >
          {visibleLogs.length === 0 ? (
            <div className="empty-state log-empty">
              <span className="log-empty-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" focusable="false">
                  <path d="M4 6h16M4 11h16M4 16h9" />
                </svg>
              </span>
              <span className="empty-state-title">{logs.length === 0 ? "No events yet" : "No matches"}</span>
              <span className="empty-state-hint">
                {logs.length === 0
                  ? "Activity streams here while a conversion runs."
                  : "Adjust the filters or search to see more."}
              </span>
            </div>
          ) : (
            visibleLogs.map((entry) => (
              <div key={entry.id} className={lineClass(entry.level)}>
                <span className="log-marker" aria-hidden="true" />
                <span className="log-ts">{entry.ts}</span>
                <span className="log-msg">{entry.msg}</span>
              </div>
            ))
          )}
        </div>

        {!following && visibleLogs.length > 0 ? (
          <button className="log-jump-latest" type="button" onClick={jumpToLatest}>
            Jump to latest
            <svg viewBox="0 0 24 24" focusable="false" aria-hidden="true">
              <path d="M12 19V6M6 12l6-6 6 6" />
            </svg>
          </button>
        ) : null}
      </div>
    </>
  );
}

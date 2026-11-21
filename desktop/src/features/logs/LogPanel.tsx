import { useEffect, useMemo, useRef, useState } from "react";

import type { EventStreamLogEntry } from "../../types/api";
import { AsciiArt } from "../../ui/ascii/AsciiArt";
import { Icon } from "../../ui/icons";
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

const LEVEL_LABELS = {
  info: "Info",
  warn: "Warn",
  error: "Error",
};

export function LogPanel({ logs }: LogPanelProps) {
  const [level, setLevel] = useState<LogLevelFilter>("all");
  const [category, setCategory] = useState<LogCategoryFilter>("all");
  const [query, setQuery] = useState("");
  const [copyStatus, setCopyStatus] = useState("");
  const [following, setFollowing] = useState(true);
  const [clearedIds, setClearedIds] = useState<Set<string | number>>(new Set());
  const scrollBoxRef = useRef<HTMLDivElement | null>(null);
  const bufferedLogs = useMemo(() => logs.filter((entry) => !clearedIds.has(entry.id)), [logs, clearedIds]);

  const visibleLogs = useMemo(
    () => filterVisibleLogs(bufferedLogs, { level, category, query }),
    [category, level, bufferedLogs, query],
  );
  const levelCounts = useMemo(() => countLogLevels(bufferedLogs), [bufferedLogs]);
  // Entries arrive newest-first, so "following" means staying pinned to the top.
  const newestEntryId = visibleLogs.length > 0 ? visibleLogs[0].id : null;
  const countLabel =
    visibleLogs.length === bufferedLogs.length
      ? `${bufferedLogs.length} ${bufferedLogs.length === 1 ? "entry" : "entries"}`
      : `${visibleLogs.length} of ${bufferedLogs.length} shown`;

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
    <div className="log-panel card">
      <div className="log-toolbar page-toolbar" aria-label="Log filters">
        <div className="seg-control control-sm log-level-control log-filter-control" role="group" aria-label="Filter by level">
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
                aria-pressed={level === value}
                onClick={() => setLevel(value)}
              >
                {label}
                {value !== "all" && count > 0 ? <span className="log-level-count tabular-nums">{count}</span> : null}
              </button>
            );
          })}
        </div>

        <TermSelect
          id="log-category-filter"
          className="log-filter-control control-sm"
          label="Filter by source"
          value={category}
          options={CATEGORY_OPTIONS}
          placeholder="All sources"
          onChange={(value) => setCategory(value as LogCategoryFilter)}
        />

        <label className="search-field log-search-wrap" htmlFor="log-search">
          <Icon name="search" className="search-icon" />
          <input
            id="log-search"
            className="field-input search-input control-sm log-search log-filter-control"
            type="search"
            placeholder="Search…"
            aria-label="Search log messages"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>

        <div className="log-toolbar-end">
          <span className="log-count tabular-nums" aria-live="polite">
            {countLabel}
          </span>
          <button
            id="log-copy-btn"
            className="btn btn-secondary btn-sm"
            type="button"
            data-tip="Copy the entries currently shown, with timestamps"
            data-tip-pos="bottom"
            onClick={() => void onCopyVisible()}
          >
            <Icon name="copy" />
            Copy
          </button>
          <button className="btn btn-ghost btn-sm" type="button" onClick={() => {
            setClearedIds(new Set(logs.map((entry) => entry.id)));
            setCopyStatus("");
          }}>
            Clear
          </button>
          <span className="field-hint log-copy-status" role="status">
            {copyStatus}
          </span>
        </div>
      </div>

      <div className="log-shell">
        <div
          id="log-box"
          ref={scrollBoxRef}
          className="panel-section log-box"
          role="log"
          tabIndex={0}
          aria-label="Event log entries"
          aria-live="polite"
          aria-relevant="additions"
          onScroll={onScroll}
        >
          {visibleLogs.length === 0 ? (
            <div className="empty-state log-empty">
              <AsciiArt className="log-empty-art" />
              <span className="empty-state-title">{bufferedLogs.length === 0 ? "No events yet" : "No matches"}</span>
              <span className="empty-state-hint">
                {bufferedLogs.length === 0
                  ? "Activity streams here while a conversion runs."
                  : "Adjust the filters or search to see more."}
              </span>
            </div>
          ) : (
            visibleLogs.map((entry) => {
              const badge = normalizeLogLevel(entry.level);
              return (
                <div key={entry.id} className={`log-line log-${badge}`}>
                  <span className="log-marker" aria-hidden="true" />
                  <span className="log-ts tabular-nums">{entry.ts}</span>
                  <span className={`log-level-badge log-level-${badge}`}>{LEVEL_LABELS[badge]}</span>
                  <span className="log-msg">{entry.msg}</span>
                </div>
              );
            })
          )}
        </div>

        {!following && visibleLogs.length > 0 ? (
          <button className="btn btn-secondary btn-sm log-jump-latest" type="button" onClick={jumpToLatest}>
            Jump to latest
            <svg className="icon" viewBox="0 0 24 24" focusable="false" aria-hidden="true">
              <path d="M12 19V6M6 12l6-6 6 6" />
            </svg>
          </button>
        ) : null}
      </div>
    </div>
  );
}

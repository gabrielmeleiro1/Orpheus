// Shared clock/duration formatting. Two families are kept intentionally
// distinct because their historical call sites render differently:
//   - formatClock*        -> always MM:SS (no hours segment)
//   - formatClockWithHours -> H:MM:SS once an hour or more has elapsed
// Do not collapse them: doing so would change rendered output somewhere.

export function formatClock(seconds: number): string {
  const whole = Math.max(0, Math.floor(Number(seconds) || 0));
  const minutes = Math.floor(whole / 60);
  const remainder = whole % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

export function formatClockWithHours(seconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainder = totalSeconds % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}

export function formatDurationClock(durationMs: number | null): string {
  if (!durationMs || durationMs <= 0) {
    return "--:--";
  }
  return formatClock(durationMs / 1000);
}

export function formatDurationClockWithHours(durationMs: number | null): string {
  if (!durationMs || durationMs <= 0) {
    return "--:--";
  }
  return formatClockWithHours(durationMs / 1000);
}

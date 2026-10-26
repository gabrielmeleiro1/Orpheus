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

/**
 * Time left, as the clock shows it: whole elapsed seconds plus whole remaining
 * seconds always add up to the whole total.
 */
export function formatRemainingClock(totalSeconds: number, elapsedSeconds: number): string {
  const whole = (value: number) => Math.max(0, Math.floor(Number.isFinite(value) ? value : 0));
  return formatClockWithHours(Math.max(0, whole(totalSeconds) - whole(elapsedSeconds)));
}

/**
 * A book's total length in words: "45 sec", "12 min", "3 hr 5 min". Like the
 * clocks it truncates, so a 43:30 book reads "43 min", never "44 min".
 */
export function formatListeningLength(seconds: number): string {
  const total = Math.max(0, Math.floor(Number.isFinite(seconds) ? seconds : 0));
  if (total < 60) return `${total} sec`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} hr ${rest} min` : `${hours} hr`;
}

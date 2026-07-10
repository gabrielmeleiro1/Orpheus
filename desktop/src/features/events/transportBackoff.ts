const DEFAULT_BASE_DELAY_MS = 500;
const DEFAULT_FACTOR = 2;
const DEFAULT_CAP_DELAY_MS = 15_000;
const QUERY_TOKEN_REFRESH_SKEW_MS = 30_000;

interface TransportBackoffOptions {
  baseDelayMs?: number;
  factor?: number;
  capDelayMs?: number;
  random?: () => number;
}

export interface TransportBackoff {
  nextDelayMs: () => number;
  reset: () => void;
}

export interface CachedQueryToken {
  token: string;
  expiresAt: number;
}

export function createTransportBackoff({
  baseDelayMs = DEFAULT_BASE_DELAY_MS,
  factor = DEFAULT_FACTOR,
  capDelayMs = DEFAULT_CAP_DELAY_MS,
  random = Math.random,
}: TransportBackoffOptions = {}): TransportBackoff {
  let attempt = 0;

  return {
    nextDelayMs: () => {
      const maxDelay = Math.min(capDelayMs, baseDelayMs * factor ** attempt);
      attempt += 1;
      return Math.round(Math.max(0, Math.min(1, random())) * maxDelay);
    },
    reset: () => {
      attempt = 0;
    },
  };
}

export function shouldRefreshQueryToken(
  token: CachedQueryToken | null,
  nowMs: number,
  skewMs = QUERY_TOKEN_REFRESH_SKEW_MS,
): boolean {
  if (!token?.token.trim()) {
    return true;
  }
  return token.expiresAt - nowMs <= skewMs;
}

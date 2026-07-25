const RUNTIME_SETUP_READY_KEY = "orpheus.runtime-setup-ready";
const RUNTIME_SETUP_READY_VERSION = 1;

type ReadyStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function browserStorage(): ReadyStorage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function readRuntimeSetupReadyHint(storage: ReadyStorage | null = browserStorage()): boolean {
  if (!storage) {
    return false;
  }
  try {
    const raw = storage.getItem(RUNTIME_SETUP_READY_KEY);
    if (!raw) {
      return false;
    }
    const parsed = JSON.parse(raw) as { version?: unknown; ready?: unknown };
    return parsed.version === RUNTIME_SETUP_READY_VERSION && parsed.ready === true;
  } catch {
    return false;
  }
}

export function writeRuntimeSetupReadyHint(
  ready: boolean,
  storage: ReadyStorage | null = browserStorage(),
): void {
  if (!storage) {
    return;
  }
  try {
    if (!ready) {
      storage.removeItem(RUNTIME_SETUP_READY_KEY);
      return;
    }
    storage.setItem(
      RUNTIME_SETUP_READY_KEY,
      JSON.stringify({ version: RUNTIME_SETUP_READY_VERSION, ready: true }),
    );
  } catch {
    // The hint is an optimization only; private/blocked storage must not stop boot.
  }
}

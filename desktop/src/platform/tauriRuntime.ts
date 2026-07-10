// Single source of truth for detecting whether the app is running inside the
// Tauri desktop shell. Previously duplicated across feature helper modules.
export function isTauriRuntime(): boolean {
  return Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

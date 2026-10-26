/// <reference types="vite/client" />

// Browser development fallback for reaching a backend outside the desktop
// shell (see readDevBackendOverride in app/useBackendClient.ts).
interface ImportMetaEnv {
  readonly VITE_ORPHEUS_BACKEND_URL?: string;
  readonly VITE_ORPHEUS_BACKEND_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

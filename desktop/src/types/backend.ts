export type DashboardTab = "convert" | "library" | "settings";

export type BackendConnectionState = "checking" | "connected" | "error";

type DesktopBackendLifecycle = "stopped" | "starting" | "ready" | "failed";

export interface DesktopBackendEndpoint {
  host: string;
  port: number;
  token: string;
  base_url: string;
}

interface DesktopBackendStatusEndpoint {
  host: string;
  port: number;
  base_url: string;
}

export interface DesktopBackendStatus {
  lifecycle: DesktopBackendLifecycle;
  endpoint: DesktopBackendStatusEndpoint | null;
  error: string | null;
}

export interface HealthResponse {
  status: string;
}

type DesktopQueryTokenScope = "events" | "audio";

export interface DesktopQueryTokenRequest {
  scope: DesktopQueryTokenScope;
  book_id?: number;
  expected_epoch?: string;
}

export interface DesktopQueryTokenResponse {
  token: string;
  expires_at: number;
}

export interface TermSelectOption {
  value: string;
  label: string;
  search?: string;
}

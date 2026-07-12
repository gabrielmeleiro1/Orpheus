export type DashboardTab = "dashboard" | "convert" | "document" | "progress" | "library" | "logs" | "settings";

export type BackendConnectionState = "checking" | "connected" | "error";

export type DesktopBackendLifecycle = "stopped" | "starting" | "ready" | "failed";

export interface DesktopBackendEndpoint {
  host: string;
  port: number;
  token: string;
  base_url: string;
}

export interface DesktopBackendStatusEndpoint {
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

export type DesktopQueryTokenScope = "events" | "audio";

export interface DesktopQueryTokenRequest {
  scope: DesktopQueryTokenScope;
  book_id?: number;
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

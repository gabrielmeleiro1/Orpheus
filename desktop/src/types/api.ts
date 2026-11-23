export * from "./backend";
export * from "./conversion";
export * from "./events";
export * from "./library";

type RuntimeProbeState = "ready" | "unavailable" | "timeout" | "pending";

type RuntimeSetupState = "checking" | "unconfigured" | "error" | "restart_required" | "ready" | "unavailable";

export interface RuntimeSetupProfile {
  device?: "cuda" | "cpu";
  group_availability?: Record<string, { available: boolean; unavailable_reason: string | null; state?: RuntimeProbeState }>;
  id: "base" | "mlx" | "cuda";
  label: string;
  description: string;
  compatible: boolean;
  reason: string | null;
  model_groups: string[];
}

interface RuntimeSetupModelGroupStatus {
  id: string;
  assets_ready: boolean;
  assets_reason: "missing" | "incomplete" | "corrupt" | "unavailable" | null;
  runtime_ready: boolean;
  ready: boolean;
}

interface RuntimeSetupPackageIssue {
  name: string;
  expected: string;
  installed: string | null;
}

interface RuntimeSetupInstalledRuntimeGroup {
  runtime_state?: RuntimeProbeState;
  runtime_reason?: string | null;
  id: string;
  runtime_ready: boolean;
  assets_ready: boolean;
  ready: boolean;
}

export interface RuntimeSetupInstalledRuntime {
  checking?: boolean;
  device?: "cuda" | "cpu";
  profile: "mlx" | "cuda";
  ready: boolean;
  runtime_available: boolean;
  model_groups: RuntimeSetupInstalledRuntimeGroup[];
}

export interface RuntimeModelDownloadStatus {
  cleaner_cuda_quantization?: string;
  configure_runtime?: boolean;
  lifecycle: "idle" | "running" | "stopping" | "cancelled" | "complete" | "failed";
  profile: string | null;
  model_groups: string[];
  total_models: number;
  completed_models: number;
  bytes_total?: number | null;
  bytes_done?: number | null;
  current_model_group: string | null;
  current_model?: string | null;
  error: string | null;
}

export interface RuntimeModelDownloadRequest {
  cleaner_cuda_quantization?: string;
  configure_runtime?: boolean;
  profile: RuntimeSetupProfile["id"];
  model_groups: string[];
}

export interface RuntimeSetupAdoptRequest {
  cleaner_cuda_quantization?: string;
  profile: RuntimeSetupProfile["id"];
  model_groups: string[];
}

export interface RuntimeSetupRuntimeDeleteResponse {
  residual_model_groups?: string[];
  message?: string;
  deleted_model_groups: string[];
  freed_bytes: number;
  state_deleted: boolean;
  /** Present when nothing residual remains and setup was reset. */
  setup_reset_id?: string;
}

export interface RuntimeModelInventory {
  models: Array<{
    id: string;
    downloaded: boolean;
    size_bytes: number;
    damaged?: boolean;
    /** Newer backends: whether Delete is allowed, and why not. */
    deletable?: boolean;
    delete_blocked?: { code: string; message: string } | null;
  }>;
}

export interface RuntimeSetupStatusResponse {
  setup_reset_id?: string;
  checking?: boolean;
  cleaner_cuda_quantization?: string;
  schema_version: 1;
  configured: boolean;
  state: RuntimeSetupState;
  state_error: string | null;
  selected_profile: RuntimeSetupProfile["id"] | null;
  selected_model_groups: RuntimeSetupModelGroupStatus[];
  recommended_profile?: RuntimeSetupProfile["id"];
  installed_runtime?: RuntimeSetupInstalledRuntime | null;
  packages: {
    ready: boolean;
    missing: RuntimeSetupPackageIssue[];
    incompatible: RuntimeSetupPackageIssue[];
  };
  probe: {
    ready: boolean;
    checks: Array<{ id: string; ready: boolean; state?: RuntimeProbeState }>;
  };
  ready: boolean;
  restart_required: boolean;
  profiles: RuntimeSetupProfile[];
  backend: {
    instance_id: string;
    status: "running";
  };
}

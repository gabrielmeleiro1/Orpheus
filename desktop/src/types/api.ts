export * from "./backend";
export * from "./conversion";
export * from "./events";
export * from "./library";

type RuntimeSetupState = "unconfigured" | "error" | "restart_required" | "ready" | "unavailable";

interface RuntimeSetupRisk {
  id: string;
  summary: string;
  accepted: boolean;
}

export interface RuntimeSetupProfile {
  id: "base" | "mlx" | "cuda";
  label: string;
  description: string;
  compatible: boolean;
  reason: string | null;
  model_groups: string[];
  risk: RuntimeSetupRisk | null;
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
  id: string;
  runtime_ready: boolean;
  assets_ready: boolean;
  ready: boolean;
}

export interface RuntimeSetupInstalledRuntime {
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
  accept_pytorch_risk: boolean;
}

export interface RuntimeSetupRuntimeDeleteResponse {
  deleted_model_groups: string[];
  freed_bytes: number;
  state_deleted: boolean;
}

export interface RuntimeModelInventory {
  models: Array<{ id: string; downloaded: boolean; size_bytes: number }>;
}

export interface RuntimeSetupStatusResponse {
  cleaner_cuda_quantization?: string;
  schema_version: 1;
  configured: boolean;
  state: RuntimeSetupState;
  state_error: "invalid" | "unavailable" | null;
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
    checks: Array<{ id: string; ready: boolean }>;
  };
  ready: boolean;
  restart_required: boolean;
  profiles: RuntimeSetupProfile[];
  risk: RuntimeSetupRisk | null;
  backend: {
    instance_id: string;
    status: "running";
  };
}

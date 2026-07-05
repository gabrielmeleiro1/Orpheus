import { ApiError, type ApiClient } from "../../api/client";
import type { SourcePreflight } from "../../types/api";

export async function runSourcePreflight(
  apiClient: ApiClient,
  sourcePath: string,
  signal?: AbortSignal,
): Promise<SourcePreflight> {
  return apiClient.preflightSource(sourcePath, { signal });
}

export function isSilentAbort(error: unknown): boolean {
  return error instanceof ApiError && (error.code === "aborted" || error.code === "timeout");
}

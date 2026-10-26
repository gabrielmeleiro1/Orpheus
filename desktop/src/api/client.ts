import { ApiError, createHttpTransport, type RequestBlob, type RequestJson } from "./http";
import { createConversionApi } from "./resources/conversion";
import { createLibraryApi } from "./resources/library";
import { createMaintenanceApi } from "./resources/maintenance";
import { createSetupApi } from "./resources/setup";
import { createSystemApi } from "./resources/system";
import type { DesktopBackendEndpoint } from "../types/api";

export { ApiError } from "./http";
export type { ApiRequestOptions } from "./http";

export function isLibraryReplacedError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409 && error.serverCode === "library_replaced";
}

interface ApiClientOptions {
  baseUrl: string;
  token?: string;
}

/**
 * The typed backend surface. It is derived from the resource factories so a
 * new endpoint is available to every consumer as soon as it is implemented.
 */
export type ApiClient = {
  usesDesktopAuth: boolean;
  requestJson: RequestJson;
  requestBlob: RequestBlob;
}
  & ReturnType<typeof createSystemApi>
  & ReturnType<typeof createSetupApi>
  & ReturnType<typeof createConversionApi>
  & ReturnType<typeof createLibraryApi>
  & ReturnType<typeof createMaintenanceApi>;

export function createApiClient({ baseUrl, token }: ApiClientOptions): ApiClient {
  const resolvedBaseUrl = baseUrl.trim();
  const bearerToken = token?.trim() ?? "";
  const usesDesktopAuth = Boolean(bearerToken);

  const { requestJson, requestBlob } = createHttpTransport(resolvedBaseUrl, bearerToken);

  return {
    usesDesktopAuth,
    requestJson,
    requestBlob,
    ...createSystemApi(requestJson),
    ...createSetupApi(requestJson),
    ...createConversionApi(requestJson),
    ...createLibraryApi({ requestJson, resolvedBaseUrl, usesDesktopAuth }),
    ...createMaintenanceApi(requestJson),
  };
}

export function createApiClientFromDesktopEndpoint(endpoint: DesktopBackendEndpoint): ApiClient {
  return createApiClient({ baseUrl: endpoint.base_url, token: endpoint.token });
}

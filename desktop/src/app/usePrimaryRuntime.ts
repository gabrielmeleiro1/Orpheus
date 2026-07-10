import { useEffect, useState } from "react";

import type { ApiClient } from "../api/client";

export function usePrimaryRuntime(apiClient: ApiClient | null): string | null {
  const [runtime, setRuntime] = useState<string | null>(null);

  useEffect(() => {
    if (!apiClient) {
      setRuntime(null);
      return undefined;
    }
    const controller = new AbortController();
    void apiClient
      .getSystemCapabilities({ signal: controller.signal })
      .then((capabilities) => {
        const value =
          typeof capabilities.primary_runtime === "string" ? capabilities.primary_runtime.trim() : "";
        setRuntime(value || null);
      })
      .catch(() => {
        setRuntime(null);
      });
    return () => {
      controller.abort();
    };
  }, [apiClient]);

  return runtime;
}

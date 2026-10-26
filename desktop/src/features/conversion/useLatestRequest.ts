import { useCallback, useEffect, useRef } from "react";

import { ApiError } from "../../api/client";
import { useGenerationGuard } from "../../state/generationGuard";

export function isAbortLikeError(error: unknown): boolean {
  // A timeout is a failed request the user can retry, not a superseded one.
  return error instanceof ApiError && error.code === "aborted";
}

export function useLatestRequest(scope?: unknown) {
  const generation = useGenerationGuard();
  const previousScope = useRef(scope);
  if (previousScope.current !== scope) {
    previousScope.current = scope;
    generation.invalidate();
  }

  useEffect(() => () => { generation.invalidate(); }, []);

  return useCallback(() => {
    generation.invalidate();
    return generation.capture();
  }, []);
}

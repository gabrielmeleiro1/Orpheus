import { useCallback, useEffect, useRef } from "react";

import { ApiError } from "../../api/client";

export function isAbortLikeError(error: unknown): boolean {
  return error instanceof ApiError && (error.code === "aborted" || error.code === "timeout");
}

export function useLatestRequest() {
  const sequenceRef = useRef(0);

  useEffect(() => {
    return () => {
      sequenceRef.current += 1;
    };
  }, []);

  const beginRequest = useCallback(() => {
    sequenceRef.current += 1;
    const sequence = sequenceRef.current;
    return () => sequenceRef.current === sequence;
  }, []);

  return beginRequest;
}

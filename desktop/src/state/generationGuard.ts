import { useEffect, useRef } from "react";

/** A capture stays current until the next explicit invalidation. */
export function createGenerationGuard() {
  const guard = {
    current: 0,
    invalidate: () => { guard.current += 1; },
    capture: (): (() => boolean) => {
      const generation = guard.current;
      return () => guard.current === generation;
    },
  };
  return guard;
}

// Lifetime and context changes belong to the caller; rendering does not invalidate.
export function useGenerationGuard() {
  const guardRef = useRef<ReturnType<typeof createGenerationGuard> | null>(null);
  if (guardRef.current === null) guardRef.current = createGenerationGuard();
  return guardRef.current;
}

export function useMountedRef(initialValue = false) {
  const mountedRef = useRef(initialValue);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  return mountedRef;
}

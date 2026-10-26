import { useCallback, useLayoutEffect, useMemo, useRef } from "react";

import type { ConversionJobController } from "../features/conversion/useConversionJob";

/** Fence every submission path, including shortcuts and queue continuation. */
export function useVerifiedConversionJob(job: ConversionJobController, ready: boolean): ConversionJobController {
  // Read at call time so a queued continuation observes the latest readiness
  // without re-creating the controller; published on commit, never in render.
  const readyRef = useRef(ready);
  useLayoutEffect(() => {
    readyRef.current = ready;
  }, [ready]);
  const submit = job.startConversion;
  const startConversion = useCallback<ConversionJobController["startConversion"]>(async (...args) => {
    if (!readyRef.current) {
      return { ok: false, status: 409, serverCode: "runtime_check_pending",
        error: "Wait for the local runtime check to finish before starting a conversion." };
    }
    return submit(...args);
  }, [submit]);
  return useMemo(() => ({ ...job, startConversion }), [job, startConversion]);
}

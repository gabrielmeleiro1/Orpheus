type ConversionPrimaryActionKind = "start_conversion" | "run_queue" | null;

export interface ConversionPrimaryActionPlan {
  primary: ConversionPrimaryActionKind;
  showRunQueue: boolean;
  showStartConversion: boolean;
  showPreflightConfirmButton: false;
}

export function resolveConversionPrimaryAction(input: {
  queueItemCount: number;
  canStartConversion: boolean;
  canRunQueue: boolean;
}): ConversionPrimaryActionPlan {
  const queueMode = input.queueItemCount > 1;
  if (queueMode) {
    return {
      primary: "run_queue",
      showRunQueue: true,
      showStartConversion: false,
      showPreflightConfirmButton: false,
    };
  }
  return {
    primary: "start_conversion",
    showRunQueue: false,
    showStartConversion: true,
    showPreflightConfirmButton: false,
  };
}

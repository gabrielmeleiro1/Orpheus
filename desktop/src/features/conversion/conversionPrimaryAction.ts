type ConversionPrimaryActionKind = "start_conversion" | "run_queue" | null;

interface ConversionPrimaryActionPlan {
  primary: ConversionPrimaryActionKind;
  showRunQueue: boolean;
  showStartConversion: boolean;
}

export function resolveConversionPrimaryAction(input: {
  queueItemCount: number;
  selectedFinished?: boolean;
  canStartConversion: boolean;
  canRunQueue: boolean;
}): ConversionPrimaryActionPlan {
  if (input.selectedFinished && input.queueItemCount > 0) {
    // Books still waiting own the primary action whenever they can start;
    // converting the selected finished book again stays available beside it.
    return {
      primary: input.canRunQueue ? "run_queue" : "start_conversion",
      showRunQueue: true,
      showStartConversion: true,
    };
  }
  if (input.queueItemCount > 1) {
    return {
      primary: "run_queue",
      showRunQueue: true,
      showStartConversion: false,
    };
  }
  return {
    primary: "start_conversion",
    showRunQueue: false,
    showStartConversion: true,
  };
}

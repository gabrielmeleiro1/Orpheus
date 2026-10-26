/** Whether this terminal state deserves a notice: only for a job this session saw in flight. */
export function resolveCompletionNotice(input: {
  previousUiState: string;
  uiState: string;
}): "completed" | "failed" | null {
  if (input.previousUiState !== "running" && input.previousUiState !== "unknown") return null;
  if (input.uiState === "completed") return "completed";
  if (input.uiState === "failed") return "failed";
  return null;
}

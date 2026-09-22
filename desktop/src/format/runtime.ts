export const RUNTIME_LABELS: Record<string, string> = { cuda: "CUDA", mlx: "MLX", cpu: "CPU" };

export function formatDevice(value: string): string {
  return value.replace(/^(cuda|mlx|cpu)(?=:|$)/i, (id) => RUNTIME_LABELS[id.toLowerCase()]);
}

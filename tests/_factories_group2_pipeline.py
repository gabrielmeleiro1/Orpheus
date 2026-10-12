"""Runtime and memory-policy doubles shared by parallel tests."""

from modules.tts.runtime_spec import TTSRuntimeSpec


class PermissiveMemoryPolicy:
    safe_mode_reason = None

    def allowed_workers(self, requested_max: int) -> int:
        return requested_max

    def should_submit_more(self, active_workers: int) -> bool:
        _ = active_workers
        return True


def cuda_runtime_spec():
    return TTSRuntimeSpec(
        provider="cuda",
        engine="kokoro",
        model="Kokoro-82M",
        quantization="bf16",
        device="cuda:0",
    )

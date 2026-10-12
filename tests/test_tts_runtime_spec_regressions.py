"""Regression coverage for tts runtime spec."""

from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from modules.tts.runtime_spec import TTSRuntimeSpec


@pytest.mark.parametrize("failure", [None, "allocation", "execution", "synchronization"])
def test_tts_cuda_executes_probe_without_cleaner_driver_gate(monkeypatch, failure):
    monkeypatch.setattr("modules.runtime_profiles.cuda_hardware_error", lambda: "CUDA 12.4 driver")
    tensor = Mock()
    tensor.add_.return_value.item.return_value = 2
    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True), ones=Mock(return_value=tensor))
    if failure == "allocation":
        torch.ones.side_effect = RuntimeError("allocation failed")
    elif failure == "execution":
        tensor.add_.side_effect = RuntimeError("kernel failed")
    elif failure == "synchronization":
        tensor.add_.return_value.item.side_effect = RuntimeError("async CUDA failure")
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert TTSRuntimeSpec.cuda_available_on_current_platform() is (failure is None)
    torch.ones.assert_called_once_with(1, device="cuda")

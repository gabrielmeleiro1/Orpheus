"""
Tests for Memory Management Features
====================================
Tests dynamic batch sizing and adaptive memory management.
"""

import types
import pytest

# Test VRAM monitor
try:
    from modules.tts.vram_monitor import (
        VRAMMonitor,
        AdaptiveMemoryManager,
        DynamicBatchConfig,
        VRAMPressureLevel,
    )
    VRAM_MONITOR_AVAILABLE = True
except ImportError:
    VRAM_MONITOR_AVAILABLE = False


pytestmark = [
    pytest.mark.memory,
]


GIB = 1024**3


def _reset_vram_singletons() -> None:
    if VRAM_MONITOR_AVAILABLE:
        VRAMMonitor._instance = None


class TestVRAMMonitor:
    """Tests for VRAM monitor functionality."""

    def setup_method(self):
        _reset_vram_singletons()

    def test_default_budget_uses_system_headroom_instead_of_fixed_32gb(self, monkeypatch):
        """Default budgets should be based on a live system/MLX memory sample."""
        memory_sample = types.SimpleNamespace(
            total_bytes=24 * GIB,
            available_bytes=6 * GIB,
            mlx_active_bytes=2 * GIB,
            mlx_cache_bytes=1 * GIB,
            mlx_peak_bytes=4 * GIB,
        )
        monkeypatch.setattr(
            "modules.tts.vram_monitor._sample_system_memory_budget",
            lambda: memory_sample,
            raising=False,
        )

        monitor = VRAMMonitor()

        assert monitor.total_vram_bytes == 24 * GIB
        assert monitor.available_vram_bytes == 7 * GIB

    def test_pressure_uses_live_system_headroom_and_mlx_cache(self, monkeypatch):
        """MLX cache still consumes unified memory and should affect pressure."""
        fake_mx = types.SimpleNamespace(
            get_active_memory=lambda: 5 * GIB,
            get_peak_memory=lambda: 6 * GIB,
            get_cache_memory=lambda: 3 * GIB,
            clear_cache=lambda: None,
        )
        memory_sample = types.SimpleNamespace(
            total_bytes=16 * GIB,
            available_bytes=2 * GIB,
            mlx_active_bytes=5 * GIB,
            mlx_cache_bytes=3 * GIB,
            mlx_peak_bytes=6 * GIB,
        )
        monkeypatch.setattr("modules.tts.vram_monitor.MLX_AVAILABLE", True)
        monkeypatch.setattr("modules.tts.vram_monitor.mx", fake_mx)
        monkeypatch.setattr(
            "modules.tts.vram_monitor._sample_system_memory_budget",
            lambda: memory_sample,
            raising=False,
        )

        monitor = VRAMMonitor()
        monitor._check_vram()

        assert monitor.get_pressure_level() == VRAMPressureLevel.CRITICAL

    def test_low_system_headroom_without_mlx_usage_is_critical(self, monkeypatch):
        """Low unified-memory headroom should reduce batching before MLX allocates."""
        fake_mx = types.SimpleNamespace(
            get_active_memory=lambda: 0,
            get_peak_memory=lambda: 0,
            get_cache_memory=lambda: 0,
            clear_cache=lambda: None,
        )
        memory_sample = types.SimpleNamespace(
            total_bytes=16 * GIB,
            available_bytes=1 * GIB,
            mlx_active_bytes=0,
            mlx_cache_bytes=0,
            mlx_peak_bytes=0,
        )
        monkeypatch.setattr("modules.tts.vram_monitor.MLX_AVAILABLE", True)
        monkeypatch.setattr("modules.tts.vram_monitor.mx", fake_mx)
        monkeypatch.setattr(
            "modules.tts.vram_monitor._sample_system_memory_budget",
            lambda: memory_sample,
            raising=False,
        )

        monitor = VRAMMonitor()
        monitor._check_vram()

        assert monitor.get_pressure_level() == VRAMPressureLevel.CRITICAL
        assert monitor.get_tts_batch_size() == monitor.config.min_tts_batch_size
    
    def test_initial_batch_sizes(self):
        """Test initial batch sizes are set correctly."""
        monitor = VRAMMonitor(total_vram_gb=16.0)
        
        assert monitor.get_tts_batch_size() == monitor.config.base_tts_batch_size
    
    def test_pressure_level_calculation(self):
        """Test pressure level calculation from usage ratio."""
        monitor = VRAMMonitor(total_vram_gb=16.0)
        
        assert monitor._calculate_pressure_level(0.5) == VRAMPressureLevel.NORMAL
        assert monitor._calculate_pressure_level(0.75) == VRAMPressureLevel.ELEVATED
        assert monitor._calculate_pressure_level(0.90) == VRAMPressureLevel.HIGH
        assert monitor._calculate_pressure_level(0.98) == VRAMPressureLevel.CRITICAL
    
    def test_batch_size_adjustment(self):
        """Test batch size adjustment based on pressure."""
        config = DynamicBatchConfig(
            base_tts_batch_size=8,
            min_tts_batch_size=1,
        )
        monitor = VRAMMonitor(total_vram_gb=16.0, config=config)
        
        # Critical pressure - should reduce significantly
        monitor._adjust_batch_sizes(VRAMPressureLevel.CRITICAL)
        critical_size = monitor.get_tts_batch_size()
        assert critical_size < config.base_tts_batch_size
        
        # Normal pressure - should restore
        monitor._adjust_batch_sizes(VRAMPressureLevel.NORMAL)
        normal_size = monitor.get_tts_batch_size()
        assert normal_size >= critical_size
    
@pytest.mark.skipif(not VRAM_MONITOR_AVAILABLE, reason="VRAM monitor not available")
class TestAdaptiveMemoryManager:
    """Tests for adaptive memory manager."""

    def setup_method(self):
        _reset_vram_singletons()

    def test_dynamic_config_updates_reused_monitor_singleton(self):
        """A later engine config should not inherit the first singleton batch size."""
        sample = types.SimpleNamespace(
            total_bytes=16 * GIB,
            available_bytes=12 * GIB,
            mlx_active_bytes=0,
            mlx_cache_bytes=0,
            mlx_peak_bytes=0,
        )

        first = AdaptiveMemoryManager(
            dynamic_config=DynamicBatchConfig(base_tts_batch_size=4),
            memory_budget_provider=lambda: sample,
        )
        second = AdaptiveMemoryManager(
            dynamic_config=DynamicBatchConfig(base_tts_batch_size=9),
            memory_budget_provider=lambda: sample,
        )

        assert first.vram_monitor is second.vram_monitor
        assert second.get_tts_batch_size() == 9

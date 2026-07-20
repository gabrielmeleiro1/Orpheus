import pytest
from pathlib import Path
import sys

# Add project root to path
PROJECT_ROOT = str(Path(__file__).parent.parent)
if PROJECT_ROOT in sys.path:
    sys.path.remove(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)


def pytest_configure(config):
    selected_args = {Path(arg).as_posix() for arg in config.args}
    if selected_args == {"tests/test_web_route_contract.py"}:
        config.option.cov_fail_under = 0


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtestloop(session):
    result = yield
    selected_args = {Path(arg).as_posix() for arg in session.config.args}
    if selected_args == {"tests/test_web_route_contract.py"}:
        cov_plugin = session.config.pluginmanager.getplugin("_cov")
        if cov_plugin is not None:
            cov_plugin.options.cov_fail_under = 0
    return result

@pytest.fixture(autouse=True)
def _deterministic_host_memory(monkeypatch):
    """Pin the host-RAM probe so worker-count math never depends on the machine.

    VRAMBudget defaults to 70% of physical RAM; on small CI runners that
    clamps max_workers to 1 and breaks any test asserting a worker count.
    Tests that exercise the clamp itself pass an explicit budget.
    """
    from modules.pipeline import parallel

    monkeypatch.setattr(parallel, "_detect_physical_ram_bytes", lambda: 32 * 1024**3)
    parallel._detect_available_ram_gb.cache_clear()
    yield
    parallel._detect_available_ram_gb.cache_clear()


@pytest.fixture
def registry_snapshot():
    """Preserve registry contents across tests that register custom backends."""
    from modules.tts.cleaner_backends.registry import _REGISTRY

    original = dict(_REGISTRY)
    yield
    _REGISTRY.clear()
    _REGISTRY.update(original)

@pytest.fixture
def test_data_dir():
    """Return path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"

@pytest.fixture
def sample_pdf(test_data_dir):
    """Return path to the committed sample PDF fixture."""
    path = test_data_dir / "sample.pdf"
    if not path.is_file():
        pytest.fail(f"Missing committed PDF fixture: {path}")
    return path

@pytest.fixture
def sample_epub(test_data_dir):
    """Return path to the committed sample EPUB fixture."""
    path = test_data_dir / "sample.epub"
    if not path.is_file():
        pytest.fail(f"Missing committed EPUB fixture: {path}")
    return path

@pytest.fixture
def mock_tts_engine(mocker):
    """Mock TTS Engine to avoid loading models."""
    mock = mocker.patch("modules.tts.engine.TTSEngine") 
    return mock

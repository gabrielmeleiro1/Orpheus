import pytest
from pathlib import Path
import sys

# Add project root to path
PROJECT_ROOT = str(Path(__file__).parent.parent)
if PROJECT_ROOT in sys.path:
    sys.path.remove(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

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

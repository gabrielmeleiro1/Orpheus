"""Keep runtime imports within their supported package boundaries."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def runtime_imports():
    imports: dict[str, set[str]] = {}
    for directory in ("modules", "apps"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            names: set[str] = set()
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    module = node.module or ""
                    names.add(module)
                    names.update(f"{module}.{alias.name}" for alias in node.names)
            imports[path.relative_to(ROOT).as_posix()] = names
    return imports


def _importers(runtime_imports, module):
    return {
        path
        for path, names in runtime_imports.items()
        if any(name == module or name.startswith(f"{module}.") for name in names)
    }


def test_runtime_modules_do_not_import_scripts(runtime_imports):
    assert _importers(runtime_imports, "scripts") == set()


@pytest.mark.parametrize("module", ["modules.tts.model_snapshots", "modules.runtime_setup.catalog"])
def test_runtime_import_configures_cache_before_hub(tmp_path, module):
    code = textwrap.dedent("""
        import importlib
        import os
        import sys
        from types import SimpleNamespace
        from modules import hf_cache

        assert 'huggingface_hub' not in sys.modules
        hf_cache.sys = SimpleNamespace(platform='win32', modules=sys.modules)
        def denied(*args, **kwargs):
            assert 'huggingface_hub' not in sys.modules
            raise OSError(1314, 'privilege not held')
        hf_cache.os.symlink = denied
        class CheckHubImport:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'huggingface_hub':
                    assert hf_cache._configured
                    assert os.environ['HF_HUB_DISABLE_SYMLINKS'] == '1'
                return None
        sys.meta_path.insert(0, CheckHubImport())
        importlib.import_module(sys.argv[1])
        assert hf_cache._configured, 'Runtime import must bootstrap the Hub cache'
        from huggingface_hub import constants
        assert constants.HF_HUB_DISABLE_SYMLINKS
        assert constants.HF_HUB_DISABLE_SYMLINKS_WARNING
    """)
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path),
               HF_HOME=str(tmp_path / "hf-home"))
    for name in (
        "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HUB_DISABLE_SYMLINKS",
        "HF_HUB_DISABLE_SYMLINKS_WARNING",
    ):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", code, module], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

"""Source setup must reach pip before any optional dependency is installed."""

import subprocess
import sys

import pytest

from scripts import setup_runtime


@pytest.mark.parametrize("journal_present", [False, True])
@pytest.mark.parametrize("cache_variable", ["HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME"])
def test_base_setup_bootstraps_without_site_packages(tmp_path, journal_present, cache_variable):
    script = """
import os
from pathlib import Path
import subprocess
import sys
from scripts import setup_runtime

state_path = Path(sys.argv[1])
journal_present = sys.argv[2] == 'True'
cache_variable = sys.argv[3]
for name in ('HF_HUB_CACHE', 'HUGGINGFACE_HUB_CACHE', 'HF_HOME', 'XDG_CACHE_HOME'):
    os.environ.pop(name, None)
cache_base = state_path.parent / 'cache'
os.environ[cache_variable] = str(cache_base)
cache_root = cache_base
if cache_variable == 'HF_HOME':
    cache_root /= 'hub'
elif cache_variable == 'XDG_CACHE_HOME':
    cache_root /= 'huggingface/hub'
cache_root.mkdir(parents=True)
if journal_present:
    (cache_root / '.orpheus-delete-pending.json').write_text('{}')

commands = []
def install(command, **kwargs):
    commands.append(command)
    return subprocess.CompletedProcess(command, 0)

try:
    setup_runtime.run_setup(profile_id='base', state_path=state_path, command_runner=install)
except setup_runtime.SetupExecutionError as exc:
    assert journal_present and 'Pending model deletion' in str(exc), str(exc)
    assert commands == []
    assert not state_path.exists()
else:
    assert not journal_present
    assert len(commands) == 1
    assert commands[0][1:4] == ['-m', 'pip', 'install']
    assert setup_runtime.read_setup_state(state_path)['profile'] == 'base'
assert 'huggingface_hub' not in sys.modules
print('bootstrap complete')
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", script, str(tmp_path / "setup.json"), str(journal_present), cache_variable],
        cwd=setup_runtime.REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        **setup_runtime.background_process_kwargs(),
    )
    assert result.returncode == 0, result.stderr
    assert "bootstrap complete" in result.stdout

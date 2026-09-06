"""Keep shared values importable without pulling in execution or persistence."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [
        ("modules.contracts", ("pydantic", "modules.storage", "modules.pipeline", "modules.tts")),
        ("modules.storage", ("modules.pipeline", "modules.tts")),
        ("modules.tts.cleaner_memory", ("modules.pipeline",)),
    ],
)
def test_core_imports_do_not_load_higher_layers(module, forbidden):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}; import sys, json; print(json.dumps(sorted(sys.modules)))"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    loaded = json.loads(result.stdout)
    assert not [
        name for name in loaded
        if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden)
    ]

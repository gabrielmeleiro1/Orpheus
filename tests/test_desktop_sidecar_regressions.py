"""Regression coverage for desktop sidecar."""

from pathlib import Path
import ast
import json


from scripts import build_desktop_backend as build


ROOT = Path(__file__).resolve().parents[1]


def test_tauri_bundle_preserves_sidecar_directory_and_license_resources():
    config = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["resources"] == {
        "binaries": "binaries", "../../LICENSE": "LICENSE",
        "../../THIRD_PARTY_LICENSES.md": "THIRD_PARTY_LICENSES.md",
    }
    assert build.TAURI_SIDECAR_RESOURCE_DIR == ROOT / "desktop/src-tauri/binaries/orpheus-desktop-backend"


def test_hf_xet_metadata_is_included_for_every_sidecar_profile():
    tree = ast.parse((ROOT / "desktop/backend-sidecar.spec").read_text(encoding="utf-8"))
    metadata = [
        node.value for node in tree.body
        if isinstance(node, ast.AugAssign)
        and isinstance(node.target, ast.Name) and node.target.id == "datas"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == "_copy_metadata"
    ]
    assert any(ast.literal_eval(call.args[0]) == "hf_xet" for call in metadata)

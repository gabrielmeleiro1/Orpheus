"""Regression coverage for the integrated setup and library flows."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from modules.web.server import create_app
from modules.tts.cleaner_backends.metadata import list_cleaner_backend_options
from scripts import setup_runtime


@pytest.mark.parametrize("title", [None, "omitted", "Updated title"])
def test_metadata_patch_allows_unchanged_title(title):
    update = Mock(return_value={"id": 1, "title": "Existing title"})
    controller = SimpleNamespace(update_book_metadata=update)
    payload = {"author": "New author", "tags": "edited"}
    if title != "omitted":
        payload["title"] = title
    with TestClient(create_app(controller=controller)) as client:
        assert client.patch("/api/library/1", json=payload).status_code == 200
    update.assert_called_once_with(
        1, title=None if title == "omitted" else title,
        author="New author", tags="edited", favorite=None,
    )


@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
def test_metadata_patch_rejects_blank_title(title):
    update = Mock()
    with TestClient(create_app(controller=SimpleNamespace(update_book_metadata=update))) as client:
        response = client.patch("/api/library/1", json={"title": title})
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Value error, Title is required"
    update.assert_not_called()


@pytest.mark.parametrize("downloaded", [False, True])
def test_no_cleaner_setup_keeps_basic_option_even_with_cached_qwen(tmp_path, monkeypatch, downloaded):
    from modules.web.routes import setup

    state_path = tmp_path / "runtime-setup.json"
    setup_runtime.write_setup_state(
        state_path, profile_id="cuda", model_groups=("tts-cpu-cuda",), accepted_risks=(),
    )
    monkeypatch.setattr(setup_runtime, "default_state_path", lambda: state_path)
    monkeypatch.setattr(setup, "_check_model_group_assets", lambda groups, **kwargs: {
        group: {"ready": downloaded} for group in groups
    })
    controller = SimpleNamespace(
        get_available_tts_engines=lambda: [], get_available_cleaner_models=lambda: [],
        get_available_api_cleaner_models=lambda: [], get_cuda_cleaner_models=lambda: [],
        get_cleaner_backend_options=list_cleaner_backend_options,
    )
    with TestClient(create_app(controller=controller)) as client:
        response = client.get("/api/options/models")
    assert response.status_code == 200
    options = {option["name"]: option for option in response.json()["cleaner_backend_options"]}
    assert options["basic"]["requires_model"] is False
    assert options["basic"]["models"] == []
    assert ("cuda" in options) is downloaded

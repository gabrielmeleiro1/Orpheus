"""Regression coverage for web options."""

from unittest.mock import Mock

from fastapi.testclient import TestClient

from modules.web.server import create_app
from scripts import setup_runtime

from tests import test_conversion_upload_lifecycle as _upload_lifecycle


controller = _upload_lifecycle.controller


def test_unreadable_setup_keeps_basic_and_api_options(controller, monkeypatch):
    monkeypatch.setattr("modules.runtime_setup.state._read_setup_state", Mock(side_effect=setup_runtime.SetupExecutionError("private state path")))
    with TestClient(create_app(controller=controller)) as client:
        response = client.get("/api/options/models")
    assert response.status_code == 200
    data = response.json()
    assert data["cleaner_models"] == data["cuda_cleaner_models"] == []
    assert {option["name"] for option in data["cleaner_backend_options"]} == {"basic", "api"}
    assert "private state path" not in response.text

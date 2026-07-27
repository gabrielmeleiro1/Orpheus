"""Frontend/backend route contract tests for the desktop API."""

from __future__ import annotations

import re
from pathlib import Path

from modules.web.server import create_app


# Keep this manifest in sync with desktop/src/api/. The test is
# intentionally limited to route existence and HTTP methods; endpoint behavior
# belongs in focused route tests.
FRONTEND_ROUTE_CONTRACT = {
    "/health": {"GET"},
    "/api/conversions": {"POST"},
    "/api/conversions/active": {"GET"},
    "/api/conversions/repair/{book_id}": {"POST"},
    "/api/conversions/{job_id}/cancel": {"POST"},
    "/api/options/models": {"GET"},
    "/api/options/openai/models": {"POST"},
    "/api/options/parallel/warmup": {"POST"},
    "/api/options/voices": {"GET"},
    "/api/setup/status": {"GET"},
    "/api/setup/adopt": {"POST"},
    "/api/setup/models/download": {"GET", "POST"},
    "/api/setup/models/download/cancel": {"POST"},
    "/api/setup/runtime": {"DELETE"},
    "/api/system/capabilities": {"GET"},
    "/api/sources/preflight": {"POST"},
    "/api/uploads/source": {"POST"},
    "/api/uploads/text": {"POST"},
    "/api/voices/suggest": {"POST"},
    "/api/library": {"GET"},
    "/api/library/count": {"GET"},
    "/api/library/{book_id}": {"GET", "PATCH", "DELETE"},
    "/api/library/{book_id}/audio": {"GET", "HEAD"},
    "/api/library/{book_id}/chapters/{chapter_number}/cleaning-preview": {"GET"},
    "/api/library/{book_id}/open-folder": {"POST"},
    "/api/library/{book_id}/playback-position": {"PUT"},
    "/api/maintenance/diagnostics": {"GET"},
    "/api/maintenance/backup": {"POST"},
    "/api/maintenance/cleanup": {"POST"},
    "/api/desktop/query-token": {"POST"},
    "/api/events/stream": {"GET"},
}


def test_frontend_api_client_routes_exist_with_expected_methods() -> None:
    app = create_app(controller=object())
    route_methods = {
        path: {method.upper() for method in operations}
        for path, operations in app.openapi()["paths"].items()
    }

    missing_routes = sorted(set(FRONTEND_ROUTE_CONTRACT) - set(route_methods))
    assert missing_routes == []

    method_mismatches = {
        path: {
            "expected": sorted(expected_methods),
            "actual": sorted(route_methods[path]),
        }
        for path, expected_methods in FRONTEND_ROUTE_CONTRACT.items()
        if not expected_methods.issubset(route_methods[path])
    }
    assert method_mismatches == {}


def _normalize_route(path: str) -> str:
    """Reduce a route to a parameter-name-agnostic shape.

    The TS client interpolates `${bookId}` where the server declares
    `{book_id}`, so both sides collapse to `{}` for comparison.
    """
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


def _client_source() -> str:
    # The API client is split across desktop/src/api/ (transport, resource
    # groups, composition); every route string in that tree belongs to the
    # contract.
    api_dir = Path(__file__).resolve().parent.parent / "desktop" / "src" / "api"
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(api_dir.rglob("*.ts"))
        if ".test." not in path.name
    )


def test_every_route_the_frontend_client_calls_is_in_the_contract() -> None:
    """The manifest's "keep in sync with desktop/src/api" comment, enforced.

    Without this, adding a client call and forgetting the manifest entry means
    renaming or deleting that server route leaves every gate green while the
    desktop app 404s.
    """
    called = {
        _normalize_route(match)
        for match in re.findall(r"[\"'`](/(?:api|health)[^\"'`]*)[\"'`]", _client_source())
    }
    assert called, "no API routes were extracted from desktop/src/api"

    contract = {_normalize_route(path) for path in FRONTEND_ROUTE_CONTRACT}
    undeclared = sorted(called - contract)

    assert undeclared == [], (
        "routes called by desktop/src/api/client.ts but missing from "
        f"FRONTEND_ROUTE_CONTRACT: {undeclared}"
    )

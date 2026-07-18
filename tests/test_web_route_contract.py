"""Frontend/backend route contract tests for the desktop API."""

from __future__ import annotations

from modules.web.server import create_app


# Keep this manifest in sync with desktop/src/api/client.ts. The test is
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
    "/api/uploads/text": {"POST"},
    "/api/voices/suggest": {"POST"},
    "/api/library": {"GET"},
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

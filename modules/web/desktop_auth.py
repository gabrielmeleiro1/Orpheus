"""Desktop-only API token validation helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, Response

_AUDIO_PLAYBACK_PATH_RE = re.compile(r"^/api/library/(?P<book_id>[^/]+)/audio/?$")
_QUERY_TOKEN_VERSION = "v1"
_QUERY_TOKEN_TTL_SECONDS = 60

desktop_query_token_router = APIRouter(prefix="/api/desktop", tags=["desktop"])


class DesktopQueryTokenRequest(BaseModel):
    scope: str
    book_id: int | None = None


class DesktopQueryTokenResponse(BaseModel):
    token: str
    expires_at: int


def _extract_bearer_token(authorization_header: str | None) -> str | None:
    if not authorization_header:
        return None

    scheme, _, value = authorization_header.partition(" ")
    if scheme.lower() != "bearer":
        return None

    token = value.strip()
    return token or None


def _is_desktop_mode(request: Request) -> bool:
    return bool(getattr(request.app.state, "desktop_mode", False))


def _path_requires_auth(path: str) -> bool:
    return path.startswith("/api/")


def _path_allows_query_token(path: str) -> bool:
    return _query_token_scope_for_path(path) is not None


def _is_valid_token(candidate: str | None, expected: str) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, expected)


def _query_token_scope_for_path(path: str) -> str | None:
    if path == "/api/events/stream":
        return "events"

    match = _AUDIO_PLAYBACK_PATH_RE.match(path)
    if match:
        return f"audio:{match.group('book_id')}"

    return None


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def _sign_query_token_payload(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    return _base64url_encode(digest)


def mint_desktop_query_token(secret: str, scope: str, ttl_seconds: int = _QUERY_TOKEN_TTL_SECONDS) -> tuple[str, int]:
    expires_at = int(time.time()) + ttl_seconds
    encoded_scope = _base64url_encode(scope.encode("utf-8"))
    nonce = secrets.token_urlsafe(12)
    payload = f"{_QUERY_TOKEN_VERSION}.{encoded_scope}.{expires_at}.{nonce}"
    signature = _sign_query_token_payload(payload, secret)
    return f"{payload}.{signature}", expires_at


def validate_desktop_query_token(candidate: str | None, secret: str, required_scope: str) -> bool:
    if not candidate:
        return False

    parts = candidate.split(".")
    if len(parts) != 5 or parts[0] != _QUERY_TOKEN_VERSION:
        return False

    _, encoded_scope, expires_at_raw, nonce, signature = parts
    if not encoded_scope or not nonce or not signature:
        return False

    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        return False

    if expires_at < int(time.time()):
        return False

    try:
        scope = _base64url_decode(encoded_scope).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return False

    if not hmac.compare_digest(scope, required_scope):
        return False

    payload = ".".join(parts[:4])
    expected_signature = _sign_query_token_payload(payload, secret)
    return hmac.compare_digest(signature, expected_signature)


def _unauthorized() -> Response:
    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


def _misconfigured() -> Response:
    return JSONResponse(status_code=500, content={"detail": "Desktop API token is not configured"})


def _request_scope(payload: DesktopQueryTokenRequest) -> str:
    scope = payload.scope.strip().lower()
    if scope == "events":
        return "events"

    if scope == "audio":
        if not payload.book_id or payload.book_id <= 0:
            raise HTTPException(status_code=422, detail="book_id is required for audio query tokens")
        return f"audio:{payload.book_id}"

    raise HTTPException(status_code=422, detail="Unsupported query token scope")


@desktop_query_token_router.post("/query-token", response_model=DesktopQueryTokenResponse)
async def create_desktop_query_token(
    payload: DesktopQueryTokenRequest,
    request: Request,
) -> DesktopQueryTokenResponse:
    if not _is_desktop_mode(request):
        raise HTTPException(status_code=404, detail="Not found")

    expected_token = getattr(request.app.state, "desktop_api_token", None)
    if not expected_token:
        raise HTTPException(status_code=500, detail="Desktop API token is not configured")

    scope = _request_scope(payload)
    token, expires_at = mint_desktop_query_token(expected_token, scope)
    return DesktopQueryTokenResponse(token=token, expires_at=expires_at)


def add_desktop_auth_middleware(app: FastAPI) -> None:
    """Add desktop API token checks for local desktop-mode requests."""

    @app.middleware("http")
    async def enforce_desktop_api_token(request: Request, call_next):  # type: ignore[no-untyped-def]
        if not _is_desktop_mode(request):
            return await call_next(request)

        if request.method.upper() == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        if not _path_requires_auth(path):
            return await call_next(request)

        expected_token = getattr(request.app.state, "desktop_api_token", None)
        if not expected_token:
            return _misconfigured()

        header_token = _extract_bearer_token(request.headers.get("authorization"))
        if _is_valid_token(header_token, expected_token):
            return await call_next(request)

        required_query_scope = _query_token_scope_for_path(path)
        if required_query_scope is not None:
            query_token = request.query_params.get("token")
            if validate_desktop_query_token(query_token, expected_token, required_query_scope):
                return await call_next(request)

        return _unauthorized()

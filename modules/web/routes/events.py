"""Event stream routes."""

from __future__ import annotations

from fastapi import APIRouter, Query
from starlette.responses import StreamingResponse

from modules.web.deps import EventBrokerDep
from modules.web.events import event_stream

router = APIRouter()


@router.get("/api/events/stream")
def stream_events(
    event_broker: EventBrokerDep,
    token: str | None = Query(default=None),
) -> StreamingResponse:
    # Desktop auth middleware validates the optional query token in desktop mode.
    _ = token
    return StreamingResponse(
        event_stream(event_broker),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

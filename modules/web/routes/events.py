"""Event stream routes."""

from __future__ import annotations

from fastapi import APIRouter, Query
from starlette.responses import StreamingResponse

from starlette.types import Receive, Scope, Send

from modules.web.deps import EventBrokerDep
from modules.web.events import close_with_response, event_stream

router = APIRouter()


class _ClosingStreamingResponse(StreamingResponse):
    """Close the frame iterator however the response ends.

    A disconnect can cancel the response while it awaits ``send`` rather than
    the next frame, which would otherwise leave the iterator suspended.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()


@router.get("/api/events/stream")
def stream_events(
    event_broker: EventBrokerDep,
    token: str | None = Query(default=None),
) -> StreamingResponse:
    # Desktop auth middleware validates the optional query token in desktop mode.
    _ = token
    return _ClosingStreamingResponse(
        close_with_response(event_stream(event_broker, announce_instance=True)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )

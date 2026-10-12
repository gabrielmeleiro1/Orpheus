"""Public error metadata independent of the HTTP transport."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, TypeAlias


PublicErrorValue: TypeAlias = str | int | float | bool | None | list["PublicErrorValue"] | dict[str, "PublicErrorValue"]


@dataclass(frozen=True)
class PublicError:
    """A stable code and user message with explicitly allowlisted safe details."""

    code: str
    message: str
    status: int
    details: Mapping[str, PublicErrorValue] | None = None


def public_error_detail(error: PublicError, *, legacy_error: bool = False) -> dict[str, PublicErrorValue]:
    """Build an HTTP detail envelope, retaining optional legacy setup keys."""
    detail = {**(error.details or {}), "code": error.code, "message": error.message}
    if legacy_error:
        detail["error"] = error.code
    return detail

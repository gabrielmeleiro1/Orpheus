"""Errors shared by runtime setup validation and execution."""

from __future__ import annotations


class SetupValidationError(ValueError):
    """Raised before mutation when a requested setup is not allowlisted."""


class SetupExecutionError(RuntimeError):
    """Raised when a fixed setup subprocess fails."""

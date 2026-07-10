"""Shared upload error types consumed by app services and web routes."""


class UploadTooLargeError(ValueError):
    """Raised when an uploaded source exceeds the configured size limit."""

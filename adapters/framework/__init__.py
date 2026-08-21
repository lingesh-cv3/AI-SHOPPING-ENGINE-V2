"""Shared adapter machinery: HTTP client, retry, idempotency, logging."""

from .http import PlatformClient

__all__ = ["PlatformClient"]
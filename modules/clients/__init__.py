"""Shared client identities split into business segments."""

from .schema import init_schema
from .services import ensure_segment

__all__ = ["ensure_segment", "init_schema"]

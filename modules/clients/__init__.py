"""Shared client identities split into business segments."""

from .schema import init_schema
from .services import create_directory_contact, ensure_segment, normalize_phone_identity
from .yclients import sync_clients

__all__ = [
    "create_directory_contact",
    "ensure_segment",
    "init_schema",
    "normalize_phone_identity",
    "sync_clients",
]

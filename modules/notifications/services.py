"""Resolve Telegram recipients and deliver notifications by business event."""

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Tuple

from .rules import notification_rule


@dataclass(frozen=True)
class DispatchResult:
    event: str
    chat_ids: Tuple[Optional[str], ...]
    statuses: Tuple[object, ...]
    used_fallback: bool


def _chat_ids_for_positions(db, positions: Tuple[str, ...]) -> Tuple[str, ...]:
    if not positions:
        return ()
    placeholders = ", ".join("?" for _ in positions)
    rows = db.execute(
        "SELECT DISTINCT eta.chat_id "
        "FROM employees e "
        "JOIN employee_positions ep ON ep.employee_id = e.id "
        "JOIN employee_telegram_accounts eta ON eta.employee_id = e.id "
        "WHERE e.deleted_at IS NULL "
        f"AND ep.position IN ({placeholders}) "
        "AND eta.chat_id != '' "
        "ORDER BY eta.chat_id",
        positions,
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def dispatch_notification(
    db,
    event: str,
    text: str,
    telegram_sender: Callable[..., object],
    *,
    fallback_chat_id: Optional[str] = None,
) -> DispatchResult:
    """Send an event immediately to every linked employee in its positions.

    The legacy group chat remains a fallback while employees are still being
    linked in the admin interface. Passing ``None`` as fallback intentionally
    lets the existing Telegram sender use TELEGRAM_CHAT_ID.
    """
    rule = notification_rule(event)
    if rule.delivery != "immediate":
        raise ValueError(f"Unsupported notification delivery: {rule.delivery}")

    chat_ids: Tuple[Optional[str], ...] = _chat_ids_for_positions(db, rule.positions)
    used_fallback = not chat_ids
    if used_fallback:
        chat_ids = (fallback_chat_id,)

    statuses = tuple(
        telegram_sender(text, chat_id=chat_id)
        for chat_id in chat_ids
    )
    return DispatchResult(
        event=event,
        chat_ids=chat_ids,
        statuses=statuses,
        used_fallback=used_fallback,
    )


def dispatch_photos(
    dispatch: DispatchResult,
    photo_paths: Iterable[str],
    telegram_photo_sender: Callable[..., object],
) -> Tuple[object, ...]:
    """Send attachments to exactly the same recipients as the text alert."""
    return tuple(
        telegram_photo_sender(photo_path, chat_id=chat_id)
        for chat_id in dispatch.chat_ids
        for photo_path in photo_paths
    )

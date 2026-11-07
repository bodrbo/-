"""Vocabulary of the order events («События» of a tuning / dealer order)."""

ORDER_KINDS = ("tuning", "dealer")

EVENT_TYPES = [
    {"value": "new_request", "label": "Новая заявка"},
    {"value": "dialog", "label": "Диалог"},
    {"value": "visit", "label": "Визит"},
]
EVENT_LABELS = {item["value"]: item["label"] for item in EVENT_TYPES}
# What the administrator adds by hand; a new request arrives by itself
# (the site's feedback form).
MANUAL_TYPES = [item for item in EVENT_TYPES if item["value"] in ("dialog", "visit")]

TEXT_MAX_LENGTH = 4000
DEFAULT_VISIT_MINUTES = 60
MIN_VISIT_MINUTES = 5
MAX_VISIT_MINUTES = 12 * 60
VISIT_DURATION_CHOICES = [
    (15, "15 минут"), (30, "30 минут"), (45, "45 минут"), (60, "1 час"),
    (90, "1,5 часа"), (120, "2 часа"), (180, "3 часа"), (240, "4 часа"),
]

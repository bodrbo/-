"""Dealer-center vocabulary."""

DEALER_STATUSES = [
    {"value": "new_request", "label": "Новая заявка"},
    {"value": "estimate", "label": "Коммерческое предложение"},
    {"value": "approved", "label": "Согласовано"},
    {"value": "awaiting_delivery", "label": "Ожидает поставки"},
    {"value": "ready", "label": "Готов к выдаче"},
    {"value": "handed_over", "label": "Передан клиенту"},
    {"value": "cancelled", "label": "Отменён"},
]
DEFAULT_STATUS = "new_request"
STATUS_LABELS = {item["value"]: item["label"] for item in DEALER_STATUSES}

# What a catalog position is. The keys are the equipment_type values of
# tuning_boat_profiles, the labels what the seller sees.
ITEM_KINDS = {"boat": "Лодка", "motor": "Мотор"}

PAYMENT_TYPES = {"CASH": "Наличные", "CARD": "Безналичный"}

DISCOUNT_TYPES = ("percent", "amount")

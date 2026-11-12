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

# Two kinds of orders live in the same tables: boats and motors from the
# catalog, and "Морские товары" — goods of the warehouse catalog.
ORDER_TYPE_EQUIPMENT = "equipment"
ORDER_TYPE_GOODS = "goods"
ORDER_TYPES = (ORDER_TYPE_EQUIPMENT, ORDER_TYPE_GOODS)

# The kind of a goods line, and the labels of every kind of a line.
GOODS_KIND = "goods"
ITEM_KIND_LABELS = {**ITEM_KINDS, GOODS_KIND: "Товар"}

# Units of the warehouse catalog (supply_products.cost_unit).
GOODS_UNITS = {"piece": "шт.", "sqm": "м²", "linear_m": "пог. м"}
DEFAULT_GOODS_UNIT = "piece"

PAYMENT_TYPES = {"CASH": "Наличные", "CARD": "Безналичный"}

DISCOUNT_TYPES = ("percent", "amount")

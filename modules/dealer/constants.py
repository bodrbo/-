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

# Orders for «Морские товары» have a group of their own. Shipping writes the
# goods off the warehouse chosen in the dialog; going back to an earlier
# status (or cancelling) returns them.
GOODS_STATUSES = [
    {"value": "new_request", "label": "Новая заявка"},
    {"value": "paid", "label": "Подтвержден и оплачен"},
    {"value": "shipped", "label": "Отгружен"},
    {"value": "completed", "label": "Завершен"},
    {"value": "cancelled", "label": "Отменён"},
]
SHIPPED_STATUS = "shipped"
GOODS_RELEASE_STATUSES = frozenset(("new_request", "paid", "cancelled"))
# Statuses an older goods order may carry from before this group existed
# -> the one that replaces them.
GOODS_LEGACY_STATUSES = {
    "estimate": "new_request", "approved": "paid", "awaiting_delivery": "paid",
    "ready": "paid", "handed_over": "completed",
}
GOODS_WRITEOFF_REASON = "Продано в заказе"

# All labels (the groups share «Новая заявка» and «Отменён») and the status
# at which an order counts as handed over, per kind of order.
STATUS_LABELS = {item["value"]: item["label"] for item in DEALER_STATUSES + GOODS_STATUSES}
FINAL_STATUS = {"equipment": "handed_over", "goods": "completed"}

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

STATUSES_BY_TYPE = {ORDER_TYPE_EQUIPMENT: DEALER_STATUSES, ORDER_TYPE_GOODS: GOODS_STATUSES}

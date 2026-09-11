"""Registry of admin-editable system settings.

Each entry has a stable ``key`` (the system_settings row), a human label,
the list of valid ``(value, label)`` choices shown as a dropdown, and a
``default`` matching whatever was previously hardcoded in app.py — so
upgrading an existing database changes nothing in production until an
admin actively picks something else on the Settings screen. Add new
settings here; the Settings page renders this registry generically, no
template changes needed.
"""

MODULKASSA_VAT_CHOICES = [
    ("1104", "НДС 0%"),
    ("1103", "НДС 10%"),
    ("1102", "НДС 20%"),
    ("1105", "НДС не облагается"),
    ("1106", "НДС с расчётной ставкой 20%"),
    ("1107", "НДС с расчётной ставкой 10%"),
    ("1109", "НДС 5%"),
    ("1110", "НДС 7%"),
    ("1111", "НДС с расчётной ставкой 5%"),
    ("1112", "НДС с расчётной ставкой 7%"),
    ("1113", "НДС 22%"),
    ("1114", "НДС с расчётной ставкой 22%"),
]

YOOKASSA_VAT_CHOICES = [
    ("1", "Без НДС"),
    ("2", "НДС по ставке 0%"),
    ("3", "НДС по ставке 10%"),
    ("4", "НДС по ставке 20%"),
    ("5", "НДС по расчётной ставке 10/110"),
    ("6", "НДС по расчётной ставке 20/120"),
    ("7", "НДС по расчётной ставке 5/105"),
    ("8", "НДС по расчётной ставке 7/107"),
]

YOOKASSA_PAYMENT_MODE_CHOICES = [
    ("full_prepayment", "Предоплата 100%"),
    ("prepayment", "Предоплата (частичная)"),
    ("advance", "Аванс"),
    ("full_payment", "Полный расчёт"),
    ("partial_payment", "Частичный расчёт и кредит"),
    ("credit", "Передача в кредит"),
    ("credit_payment", "Оплата кредита"),
]

# "default" values are strings on purpose — settings are stored and read as
# plain text, and callers parse/validate on the way out.
SETTINGS_GROUPS = [
    {
        "id": "vat",
        "title": "НДС и фискальные чеки",
        "hint": (
            "Коды ставки НДС, которые уходят в фискальные чеки при разных "
            "видах оплаты. Меняйте только по согласованию с бухгалтером — "
            "это влияет на реальные чеки, отправляемые в ФНС."
        ),
        "settings": {
            "modulkassa_vat_tag": {
                "label": "МодульКасса — ручные платежи по заказам тюнинг-центра",
                "hint": "Чек пробивается при ручной регистрации платежа на странице заказа.",
                "choices": MODULKASSA_VAT_CHOICES,
                "default": "1109",
            },
            "yookassa_vat_code": {
                "label": "ЮKassa — онлайн-оплата заказов тюнинг-центра",
                "hint": "Применяется к счёту, который клиент оплачивает по ссылке ЮKassa.",
                "choices": YOOKASSA_VAT_CHOICES,
                "default": "7",
            },
            "yookassa_excursion_vat_code": {
                "label": "ЮKassa — возвраты по экскурсионным рейсам",
                "hint": "Если не задано, используется то же значение, что и для тюнинг-центра.",
                "choices": YOOKASSA_VAT_CHOICES,
                "default": "",
                "allow_empty": True,
                "empty_label": "Как для тюнинг-центра",
            },
            "yookassa_excursion_payment_mode": {
                "label": "ЮKassa — признак способа расчёта в чеках возврата",
                "hint": "Технический параметр формирования чека, обычно не требует изменения.",
                "choices": YOOKASSA_PAYMENT_MODE_CHOICES,
                "default": "full_prepayment",
            },
        },
    },
]


def all_settings():
    """Flat {key: definition} view of every setting across every group."""
    result = {}
    for group in SETTINGS_GROUPS:
        result.update(group["settings"])
    return result

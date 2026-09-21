"""The toggleable module list a platform admin picks from when branding a
demo account, and the mapping used to gate requests against it. Anything
not covered by either map below is treated as core and always allowed —
Сотрудники, Настройки, the home page, auth routes. The shared client
directory is gated at segment level in app.py because its tuning and
excursion sections live behind the same URL."""

DEMO_MODULES = [
    {"key": "tuning", "label": "Тюнинг-центр"},
    {"key": "fleet", "label": "Флот"},
    {"key": "excursions", "label": "Морские прогулки"},
    {"key": "analytics", "label": "Аналитика"},
    {"key": "supply", "label": "Снабжение"},
    {"key": "assistant", "label": "AI-помощник"},
]
DEMO_MODULE_KEYS = frozenset(m["key"] for m in DEMO_MODULES)
DEMO_MODULE_LABELS = {m["key"]: m["label"] for m in DEMO_MODULES}

# Blueprint-based sections (Flask exposes request.blueprint directly).
DEMO_MODULE_BLUEPRINTS = {
    "fleet": ("fleet",),
    "excursions": ("schedule", "excursion_services"),
    "assistant": ("ai_assistant",),
}

# Monolithic (non-blueprint) sections in app.py, matched by URL path
# prefix instead — "Морские прогулки" also covers the legacy investor
# revenue-split pages at /trips. "/schedule/tuning" is the tuning_schedule
# blueprint (deliberately its own blueprint, not part of "schedule", so it
# gates here by path instead of being swept into "excursions" — see
# module_for_request in services.py, which checks DEMO_MODULE_BLUEPRINTS
# first and only falls through to this map when the blueprint isn't
# listed there).
DEMO_MODULE_PATH_PREFIXES = {
    "tuning": ("/tuning", "/schedule/tuning"),
    "fleet": ("/fleet",),
    "excursions": ("/trips",),
    "analytics": ("/analytics",),
    "supply": ("/supply",),
}

DEMO_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico"}

# Guided-tour script shown to a demo tenant the first time we see a new IP
# address for that account (see services.note_login_ip / routes.tour_next).
# Each step names the Flask endpoint it belongs to (the card only renders
# when the current request resolves to that endpoint — see
# _demo_tour_active_step in app.py) and one or more `data-tour-target`
# selectors on that page to spotlight. Edit this list to add/reorder/retext
# steps — nothing else needs to change.
DEMO_TOUR_STEPS = [
    {
        "endpoint": "tuning_index",
        "targets": ["[data-tour-target='nav-tuning']"],
        "title": "Тюнинг-центр",
        "text": "Главный раздел системы «Тюнинг-центр» — здесь происходит вся работа с заказами.",
    },
    {
        "endpoint": "tuning_index",
        "targets": ["[data-tour-target='tuning-subnav-orders']"],
        "title": "Заказы",
        "text": (
            "В подразделе «Заказы» выдаётся полный список всех заказов — тут и "
            "завершённые, и активные заказы с возможностью отфильтровать их по "
            "любым данным клиента, модели лодки/мотора, статусу, номеру заказа "
            "или дате."
        ),
    },
    {
        "endpoint": "tuning_index",
        "targets": ["[data-tour-target='tuning-filters']"],
        "title": "Фильтры",
        "text": "Вот здесь можно отфильтровать список заказов по разным параметрам.",
    },
    {
        "endpoint": "tuning_index",
        "targets": ["[data-tour-target='tuning-add-order']"],
        "title": "Новый заказ",
        "text": "Добавить заказ можно по этой кнопке.",
    },
    {
        "endpoint": "tuning_index",
        "targets": [
            "[data-tour-target='tuning-select-all']",
            "[data-tour-target='tuning-bulk-panel']",
        ],
        "title": "Массовые действия",
        "text": "Здесь можно массово изменить заказам статусы или удалить ненужные заказы.",
    },
    {
        "endpoint": "tuning_index",
        "targets": ["[data-tour-target='tuning-order-number-column']"],
        "title": "№ заказа",
        "text": (
            "Номер заказа кликабельный, при нажатии вас перебросит в карточку "
            "заказа, где можно будет посмотреть/изменить список работ и "
            "сопутствующих товаров."
        ),
    },
    {
        "endpoint": "tuning_index",
        "targets": ["[data-tour-target='tuning-order-date-column']"],
        "title": "Дата",
        "text": (
            "В поле «Дата» можно увидеть две даты: первая — дата создания "
            "заказа, вторая — дата предполагаемого завершения заказа. Когда "
            "заказ будет выполнен, вторая дата автоматически заменится на "
            "дату фактического завершения заказа. Если заказ просрочен, дата "
            "фактического завершения окрасится в красный цвет, а при "
            "наведении покажет количество дней просрочки."
        ),
    },
]

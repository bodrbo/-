"""The toggleable module list a platform admin picks from when branding a
demo account, and the mapping used to gate requests against it. Anything
not covered by either map below is treated as core and always allowed —
Сотрудники, Настройки, Клиенты и партнеры, the home page, auth routes."""

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
# revenue-split pages at /trips.
DEMO_MODULE_PATH_PREFIXES = {
    "tuning": ("/tuning",),
    "fleet": ("/fleet",),
    "excursions": ("/trips",),
    "analytics": ("/analytics",),
    "supply": ("/supply",),
}

DEMO_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".webp"}

"""Business constants for the internal schedule."""

ITEM_KINDS = {
    "booking": "Аренда катера",
    "event": "Групповая экскурсия",
}

CREW_ROLES = {
    "captain": "Капитан",
    "guide": "Гид",
    "guide_captain": "Гид-капитан",
}

DEFAULT_DAY_START_HOUR = 8
DEFAULT_DAY_END_HOUR = 22
MIN_ITEM_MINUTES = 30
MAX_ITEM_HOURS = 12

# One-time seed for schedule_service_rates (₽/hour by service name + crew
# role) — copied from app.py's WORK_TYPES, which is what payroll has always
# paid for these exact trip types. Kept here rather than imported from
# app.py so this module stays self-contained, same as DEFAULT_SERVICES in
# modules/excursion_services/constants.py. No "guide" row for any service:
# WORK_TYPES never had a rate for a guide working alongside a separate
# captain, and guessing one here would hide a real gap instead of
# surfacing it (see modules/schedule/services.py::auto_close_schedule_items,
# which flags this case with needs_review rather than defaulting silently).
# After seeding, this table is the live source of truth — edit rates via
# /services/rates, not by changing this list.
DEFAULT_SERVICE_RATES = [
    ("Малый тур", "captain", 1100),
    ("Малый тур", "guide_captain", 1870),
    ("Средний тур", "captain", 1100),
    ("Средний тур", "guide_captain", 1870),
    ("Большой тур", "captain", 1100),
    ("Большой тур", "guide_captain", 1870),
    ("Аренда на 3 часа", "captain", 1100),
    ("Индивидуальная аренда 1 час", "captain", 1100),
    ("Индивидуальная аренда на 1.5 часа", "captain", 1100),
    ("Индивидуальная аренда 2 часа", "captain", 1100),
    ("Индивидуальная аренда на 2.5 часа", "captain", 1100),
]

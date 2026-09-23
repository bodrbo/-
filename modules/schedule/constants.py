"""Business constants for the internal schedule."""

ITEM_KINDS = {
    "booking": "Индивидуальная экскурсия",
    "event": "Групповая экскурсия",
}

CREW_ROLES = {
    "captain": "Капитан",
    "guide": "Гид",
    "guide_captain": "Гид-капитан",
}

DEFAULT_DAY_START_HOUR = 8
DEFAULT_DAY_END_HOUR = 22
TIME_STEP_MINUTES = 15
MIN_ITEM_MINUTES = TIME_STEP_MINUTES
MAX_ITEM_HOURS = 12

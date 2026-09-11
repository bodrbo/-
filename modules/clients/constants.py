"""Client directory segments."""

TUNING_SEGMENT = "tuning"
EXCURSION_SEGMENT = "excursion"
CLIENT_SEGMENTS = (TUNING_SEGMENT, EXCURSION_SEGMENT)

CLIENT_RELATIONSHIP_CLIENT = "client"
CLIENT_RELATIONSHIP_PARTNER = "partner"
CLIENT_RELATIONSHIP_TYPES = (
    CLIENT_RELATIONSHIP_CLIENT,
    CLIENT_RELATIONSHIP_PARTNER,
)
CLIENT_RELATIONSHIP_OPTIONS = (
    {"value": CLIENT_RELATIONSHIP_CLIENT, "label": "Клиент"},
    {"value": CLIENT_RELATIONSHIP_PARTNER, "label": "Партнёр"},
)

TRIPSTER_CHANNEL = "tripster"
CLIENT_ACQUISITION_CHANNELS = (
    {"value": TRIPSTER_CHANNEL, "label": "Трипстер"},
    {"value": "sputnik", "label": "Спутник"},
    {"value": "bodrbo_fort", "label": "Сайт bodrbo-fort.ru"},
)

CLIENT_CONTACT_METHODS = (
    {"value": "telegram", "label": "Telegram"},
    {"value": "whatsapp", "label": "WhatsApp"},
    {"value": "max", "label": "MAX"},
    {"value": "email", "label": "Почта"},
)

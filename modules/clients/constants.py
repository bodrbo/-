"""Client directory segments."""

TUNING_SEGMENT = "tuning"
EXCURSION_SEGMENT = "excursion"
CLIENT_SEGMENTS = (TUNING_SEGMENT, EXCURSION_SEGMENT)

TRIPSTER_CHANNEL = "tripster"
CLIENT_ACQUISITION_CHANNELS = (
    {"value": TRIPSTER_CHANNEL, "label": "Трипстер"},
    {"value": "sputnik", "label": "Спутник"},
    {"value": "bodrbo_fort", "label": "Сайт bodrbo-fort.ru"},
)

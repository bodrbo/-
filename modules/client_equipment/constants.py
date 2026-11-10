"""Vocabulary of the client's own equipment."""

KINDS = {"boat": "Лодка", "motor": "Мотор"}
DEFAULT_KIND = "boat"

# What the staff marks the position with: the one the client uses now or the
# one he no longer has (sold, written off).
STATUSES = {"active": "Актуальна", "archive": "Архив"}
DEFAULT_STATUS = "active"

NAME_MAX = 120
SERIAL_MAX = 60
NOTE_MAX = 300

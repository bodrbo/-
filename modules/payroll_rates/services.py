"""Validation for payroll pay rate edits."""

import datetime as dt

from . import repository
from .constants import EXCURSION_ROLES


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def set_excursion_role_rate(db, role, raw_rate):
    if role not in EXCURSION_ROLES:
        return False, "Некорректная роль."
    raw_rate = str(raw_rate or "").strip().replace(",", ".")
    try:
        rate = float(raw_rate)
    except ValueError:
        return False, "Ставка должна быть числом."
    if rate < 0:
        return False, "Ставка не может быть отрицательной."
    repository.set_excursion_role_rate(db, role, rate, current_timestamp())
    return True, f"Ставка «{EXCURSION_ROLES[role]}» обновлена."

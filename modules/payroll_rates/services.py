"""Validation for payroll pay rate edits."""

import datetime as dt

from . import repository
from .constants import BOAT_RATE_ROLES, EXCURSION_ROLES


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


def parse_boat_rates(role, raw_rates, known_boats):
    """raw_rates: [(boat, text)]. A blank text means "no own rate" (0).
    Returns (error, {boat: rate})."""
    if role not in BOAT_RATE_ROLES:
        return "Для этой должности ставки по катерам не ведутся.", {}
    rates = {}
    for boat, raw in raw_rates:
        if boat not in known_boats:
            return f"Неизвестный катер «{boat}».", {}
        text = str(raw or "").strip().replace(",", ".")
        if not text:
            rates[boat] = 0.0
            continue
        try:
            rate = float(text)
        except ValueError:
            return f"Ставка катера «{boat}» должна быть числом.", {}
        if rate < 0:
            return f"Ставка катера «{boat}» не может быть отрицательной.", {}
        rates[boat] = rate
    return None, rates


def save_boat_rates(db, role, raw_rates, known_boats, recalculate, today=None):
    """Saves the per-boat rates of a role. The first save that sets at least
    one rate also recalculates the last week's already-accrued pay by them
    (`recalculate(db, role, since_date)` -> notice text, injected from app.py
    because it reaches trips and payroll entries). Returns (ok, message)."""
    error, rates = parse_boat_rates(role, raw_rates, known_boats)
    if error:
        return False, error
    first_time = not repository.boat_rates_applied(db, role)
    repository.set_boat_rates(db, role, rates, current_timestamp())
    message = f"Ставки «{EXCURSION_ROLES[role]}» по катерам сохранены."
    if first_time and any(rates.values()):
        today = today or dt.date.today()
        since = (today - dt.timedelta(days=7)).isoformat()
        repository.mark_boat_rates_applied(db, role, current_timestamp())
        details = recalculate(db, role, since)
        if details:
            message += " " + details
    return True, message

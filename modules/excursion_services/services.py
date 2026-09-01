"""Validation and use cases for excursion products."""

import datetime as dt
import sqlite3

from . import repository


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _normalise_name(raw_name):
    return " ".join(str(raw_name or "").strip().split())[:180]


def _parse_number(raw_value, label, minimum, maximum, errors):
    value_text = str(raw_value or "").strip().replace(" ", "").replace(",", ".")
    try:
        value = float(value_text)
    except (TypeError, ValueError):
        errors.append(f"{label} должна быть числом.")
        return 0.0
    if value < minimum or value > maximum:
        errors.append(f"{label} должна быть от {minimum:g} до {maximum:g}.")
    return value


def validate_form(db, form, service_id=None):
    errors = []
    name = _normalise_name(form.get("name"))
    if len(name) < 2:
        errors.append("Укажите название услуги.")
    hours = _parse_number(
        form.get("hours"), "Длительность", 0.5, 24, errors
    )
    price = _parse_number(form.get("price"), "Цена", 0, 10_000_000, errors)
    existing = repository.get_service_by_name(db, name) if name else None
    if existing is not None and existing["id"] != service_id:
        errors.append("Услуга с таким названием уже есть в каталоге.")
    return errors, {"name": name, "hours": hours, "price": price}


def create_service(db, form):
    errors, data = validate_form(db, form)
    if errors:
        return False, " ".join(errors), data
    try:
        service_id = repository.create_service(db, data, current_timestamp())
    except sqlite3.IntegrityError:
        return False, "Услуга с таким названием уже есть в каталоге.", data
    return True, f"Услуга «{data['name']}» добавлена.", service_id


def update_service(db, service_id, form):
    if repository.get_service(db, service_id) is None:
        return False, "Услуга не найдена.", None
    errors, data = validate_form(db, form, service_id=service_id)
    if errors:
        return False, " ".join(errors), data
    try:
        updated = repository.update_service(
            db, service_id, data, current_timestamp()
        )
    except sqlite3.IntegrityError:
        return False, "Услуга с таким названием уже есть в каталоге.", data
    if not updated:
        return False, "Услуга не найдена.", data
    return True, f"Услуга «{data['name']}» обновлена.", data

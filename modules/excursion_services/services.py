"""Validation and use cases for excursion products."""

import datetime as dt
import sqlite3

from . import repository
from .constants import SERVICE_TYPES


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


def _parse_tripster_id(raw_value, errors):
    value_text = str(raw_value or "").strip()
    if not value_text:
        return None
    try:
        value = int(value_text)
    except (TypeError, ValueError):
        errors.append("Tripster ID должен быть целым числом.")
        return None
    if value <= 0:
        errors.append("Tripster ID должен быть больше нуля.")
        return None
    return value


def _boat_prices_from_form(form, boats, errors):
    submitted_names = form.getlist("boat_name[]")
    submitted_prices = form.getlist("boat_price[]")
    submitted = {
        name: submitted_prices[index] if index < len(submitted_prices) else ""
        for index, name in enumerate(submitted_names)
    }
    result = {}
    for boat in boats:
        boat_name = boat["name"]
        result[boat_name] = _parse_number(
            submitted.get(boat_name, "0"),
            f"Цена для катера «{boat_name}»",
            0,
            10_000_000,
            errors,
        )
    return result


def validate_form(db, form, boats, service_id=None):
    errors = []
    name = _normalise_name(form.get("name"))
    if len(name) < 2:
        errors.append("Укажите название услуги.")
    hours = _parse_number(
        form.get("hours"), "Длительность", 0.5, 24, errors
    )
    service_type = str(form.get("service_type") or "group").strip()
    if service_type not in SERVICE_TYPES:
        errors.append("Выберите категорию услуги.")
        service_type = "group"
    if service_type == "group":
        price = _parse_number(form.get("price"), "Цена", 0, 10_000_000, errors)
        boat_prices = {}
    else:
        price = 0.0
        boat_prices = _boat_prices_from_form(form, boats, errors)
    tripster_id = _parse_tripster_id(form.get("tripster_id"), errors)
    existing = repository.get_service_by_name(db, name) if name else None
    if existing is not None and existing["id"] != service_id:
        errors.append("Услуга с таким названием уже есть в каталоге.")
    tripster_service = repository.get_service_by_tripster_id(db, tripster_id)
    if tripster_service is not None and tripster_service["id"] != service_id:
        errors.append(
            "Этот Tripster ID уже указан у услуги "
            f"«{tripster_service['name']}»."
        )
    return errors, {
        "name": name,
        "service_type": service_type,
        "tripster_id": tripster_id,
        "hours": hours,
        "price": price,
        "boat_prices": boat_prices,
    }


def validate_addon_product_form(db, form, product_id=None):
    errors = []
    name = _normalise_name(form.get("name"))
    if len(name) < 2:
        errors.append("Укажите название товара.")
    cost_price = _parse_number(
        form.get("cost_price"), "Себестоимость", 0, 10_000_000, errors
    )
    sale_price = _parse_number(
        form.get("sale_price"), "Цена продажи", 0, 10_000_000, errors
    )
    raw_auto_add = str(form.get("auto_add_service_id") or "").strip()
    auto_add_service_id = None
    if raw_auto_add:
        try:
            auto_add_service_id = int(raw_auto_add)
        except ValueError:
            errors.append("Некорректная услуга для автодобавления.")
        else:
            if repository.get_service(db, auto_add_service_id) is None:
                errors.append("Услуга для автодобавления не найдена.")
    existing = repository.get_addon_product_by_name(db, name) if name else None
    if existing is not None and existing["id"] != product_id:
        errors.append("Товар с таким названием уже есть в каталоге.")
    return errors, {
        "name": name,
        "cost_price": cost_price,
        "sale_price": sale_price,
        "auto_add_service_id": auto_add_service_id,
    }


def create_addon_product(db, form):
    errors, data = validate_addon_product_form(db, form)
    if errors:
        return False, " ".join(errors), data
    try:
        product_id = repository.create_addon_product(db, data, current_timestamp())
    except sqlite3.IntegrityError:
        return False, "Название уже используется.", data
    return True, f"Товар «{data['name']}» добавлен.", product_id


def update_addon_product(db, product_id, form):
    if repository.get_addon_product(db, product_id) is None:
        return False, "Товар не найден.", None
    errors, data = validate_addon_product_form(db, form, product_id=product_id)
    if errors:
        return False, " ".join(errors), data
    try:
        updated = repository.update_addon_product(
            db, product_id, data, current_timestamp()
        )
    except sqlite3.IntegrityError:
        return False, "Название уже используется.", data
    if not updated:
        return False, "Товар не найден.", data
    return True, f"Товар «{data['name']}» обновлён.", data


def create_service(db, form, boats):
    errors, data = validate_form(db, form, boats)
    if errors:
        return False, " ".join(errors), data
    try:
        service_id = repository.create_service(db, data, current_timestamp())
    except sqlite3.IntegrityError:
        return False, "Название или Tripster ID уже используются.", data
    return True, f"Услуга «{data['name']}» добавлена.", service_id


def update_service(db, service_id, form, boats):
    if repository.get_service(db, service_id) is None:
        return False, "Услуга не найдена.", None
    errors, data = validate_form(db, form, boats, service_id=service_id)
    if errors:
        return False, " ".join(errors), data
    try:
        updated = repository.update_service(
            db, service_id, data, current_timestamp()
        )
    except sqlite3.IntegrityError:
        return False, "Название или Tripster ID уже используются.", data
    if not updated:
        return False, "Услуга не найдена.", data
    return True, f"Услуга «{data['name']}» обновлена.", data

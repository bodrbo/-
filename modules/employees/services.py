"""Employee-directory business rules independent from HTTP handlers."""

import datetime as dt
import html

from . import repository
from .constants import POSITION_MAX_LENGTH


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _initials(name):
    parts = [part for part in name.split() if part]
    return "".join(part[0].upper() for part in parts[:2]) or "?"


def employee_directory(db):
    positions_by_employee = {}
    for position in repository.list_employee_positions(db):
        positions_by_employee.setdefault(position["employee_id"], []).append(position)

    links_by_employee = {
        link["employee_id"]: link for link in repository.list_telegram_links(db)
    }
    employees = []
    for row in repository.list_employees(db):
        employee = dict(row)
        employee["initials"] = _initials(employee["name"])
        employee["positions"] = positions_by_employee.get(employee["id"], [])
        employee["telegram"] = links_by_employee.get(employee["id"])
        employees.append(employee)
    return employees


def telegram_contacts(db):
    return [dict(contact) for contact in repository.list_telegram_contacts(db)]


def add_position(db, employee_id, raw_position):
    employee = repository.get_employee(db, employee_id)
    if employee is None:
        return False, "Сотрудник не найден."

    position = " ".join((raw_position or "").strip().split())
    if not position:
        return False, "Введите название должности."
    if len(position) > POSITION_MAX_LENGTH:
        return False, f"Название должности должно быть короче {POSITION_MAX_LENGTH + 1} символов."
    if any(ord(character) < 32 for character in position):
        return False, "Название должности содержит недопустимые символы."

    existing = {
        item["position"].casefold()
        for item in repository.list_employee_positions(db)
        if item["employee_id"] == employee_id
    }
    if position.casefold() in existing:
        return False, "Эта должность уже назначена сотруднику."

    repository.add_position(db, employee_id, position, current_timestamp())
    return True, f"Должность «{position}» добавлена сотруднику {employee['name']}."


def delete_position(db, employee_id, position_id):
    employee = repository.get_employee(db, employee_id)
    position = repository.get_position(db, employee_id, position_id)
    if employee is None or position is None:
        return False, "Должность не найдена."
    repository.delete_position(db, employee_id, position_id)
    return True, f"Должность «{position['position']}» удалена у сотрудника {employee['name']}."


def sync_telegram_contacts(db, contacts):
    synced = 0
    timestamp = current_timestamp()
    for raw_contact in contacts:
        chat_id = str(raw_contact.get("chat_id") or "").strip()
        if not chat_id:
            continue
        contact = {
            "chat_id": chat_id[:80],
            "username": str(raw_contact.get("username") or "").strip().lstrip("@")[:100],
            "display_name": str(raw_contact.get("display_name") or "").strip()[:200],
            "last_text": str(raw_contact.get("last_text") or "").strip()[:500],
            "last_message_at": raw_contact.get("last_message_at"),
        }
        repository.upsert_telegram_contact(db, contact, timestamp)
        synced += 1
    repository.commit(db)
    return synced


def link_telegram_account(db, employee_id, chat_id):
    employee = repository.get_employee(db, employee_id)
    if employee is None:
        return False, "Сотрудник не найден."
    contact = repository.get_telegram_contact(db, str(chat_id or "").strip())
    if contact is None:
        return False, "Telegram-аккаунт не найден. Сначала обновите список контактов."

    occupied = repository.find_link_by_chat_id(db, contact["chat_id"])
    if occupied is not None and occupied["employee_id"] != employee_id:
        return False, f"Этот Telegram уже привязан к сотруднику {occupied['employee_name']}."

    repository.link_telegram_account(db, employee, contact, current_timestamp())
    label = f"@{contact['username']}" if contact["username"] else contact["display_name"]
    return True, f"Telegram {label or contact['chat_id']} привязан к сотруднику {employee['name']}."


def unlink_telegram_account(db, employee_id):
    employee = repository.get_employee(db, employee_id)
    if employee is None:
        return False, "Сотрудник не найден."
    if repository.get_telegram_link(db, employee_id) is None:
        return False, "У сотрудника нет привязанного Telegram."
    repository.unlink_telegram_account(db, employee)
    return True, f"Telegram отвязан от сотрудника {employee['name']}."


def send_test_notification(db, employee_id, telegram_sender):
    employee = repository.get_employee(db, employee_id)
    link = repository.get_telegram_link(db, employee_id)
    if employee is None or link is None:
        return False, "Сначала привяжите Telegram-аккаунт."

    result = telegram_sender(
        link["chat_id"],
        f"🔔 <b>Тестовое уведомление</b>\n{html.escape(employee['name'])}, связь с системой настроена.",
    )
    if result == "sent":
        return True, f"Тестовое уведомление отправлено сотруднику {employee['name']}."
    return False, f"Telegram не подтвердил отправку: {result}"


def telegram_chat_id_for_employee(db, employee_name):
    return repository.get_telegram_chat_id_by_employee_name(db, employee_name)

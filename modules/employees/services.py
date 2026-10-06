"""Employee-directory business rules independent from HTTP handlers."""

import datetime as dt
import html
import re
import secrets
import unicodedata

from werkzeug.security import generate_password_hash

from . import repository
from .constants import (
    ADMIN_NAME_MAX_LENGTH,
    EMPLOYEE_LOGIN_MIN_LENGTH,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    CANDIDATE_NAME_MAX_LENGTH,
    CANDIDATE_NOTE_MAX_LENGTH,
    CANDIDATE_PHONE_MAX_LENGTH,
    KNOWN_POSITIONS,
    EMPLOYEE_LOGIN_MAX_LENGTH,
    EMPLOYEE_NAME_MAX_LENGTH,
    GENERATED_PASSWORD_LENGTH,
    POSITION_MAX_LENGTH,
)


_CYRILLIC_TRANSLITERATION = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e",
    "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _initials(name):
    parts = [part for part in name.split() if part]
    return "".join(part[0].upper() for part in parts[:2]) or "?"


def active_employee_names(db):
    return repository.list_active_employee_names(db)


def known_positions(db):
    return sorted(
        set(KNOWN_POSITIONS) | set(repository.list_known_positions(db)),
        key=str.casefold,
    )


def _normalise_position(raw_position):
    position = " ".join((raw_position or "").strip().split())
    if not position:
        return None, "Введите название должности."
    if len(position) > POSITION_MAX_LENGTH:
        return None, (
            f"Название должности должно быть короче {POSITION_MAX_LENGTH + 1} символов."
        )
    if any(ord(character) < 32 for character in position):
        return None, "Название должности содержит недопустимые символы."
    return position, None


def _normalise_employee_name(raw_name):
    name = " ".join((raw_name or "").strip().split())
    if not name:
        return None, "Введите ФИО сотрудника."
    if len(name) > EMPLOYEE_NAME_MAX_LENGTH:
        return None, f"ФИО должно быть не длиннее {EMPLOYEE_NAME_MAX_LENGTH} символов."
    if len(name.split()) < 2:
        return None, "Укажите как минимум имя и фамилию."
    if any(not (character.isalpha() or character in " -'’") for character in name):
        return None, "ФИО может содержать только буквы, пробелы, дефисы и апострофы."
    return name, None


def _login_base(name):
    transliterated = "".join(
        _CYRILLIC_TRANSLITERATION.get(character.casefold(), character.casefold())
        for character in name
    )
    transliterated = unicodedata.normalize("NFKD", transliterated)
    ascii_text = transliterated.encode("ascii", "ignore").decode("ascii")
    parts = [
        "".join(character for character in part if character.isalnum())
        for part in ascii_text.replace("-", " ").replace("'", " ").split()
    ]
    parts = [part for part in parts if part]
    if len(parts) >= 2:
        base = f"{parts[0]}.{parts[-1]}"
    elif parts:
        base = parts[0]
    else:
        base = "crew"
    if len(base) < 3:
        base = f"crew.{base}"
    return base[:EMPLOYEE_LOGIN_MAX_LENGTH].strip(".")


def _unique_login(db, name):
    base = _login_base(name)
    candidate = base
    suffix = 2
    while repository.team_username_exists(db, candidate):
        suffix_text = str(suffix)
        candidate = f"{base[:EMPLOYEE_LOGIN_MAX_LENGTH - len(suffix_text) - 1]}.{suffix_text}"
        suffix += 1
    return candidate


def _generate_password():
    return "".join(
        secrets.choice(_PASSWORD_ALPHABET) for _ in range(GENERATED_PASSWORD_LENGTH)
    )


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

    position, error = _normalise_position(raw_position)
    if error:
        return False, error

    existing = {
        item["position"].casefold()
        for item in repository.list_employee_positions(db)
        if item["employee_id"] == employee_id
    }
    if position.casefold() in existing:
        return False, "Эта должность уже назначена сотруднику."

    repository.add_position(db, employee_id, position, current_timestamp())
    return True, f"Должность «{position}» добавлена сотруднику {employee['name']}."


def create_employee(db, raw_name, raw_positions, raw_custom_position, raw_chat_id):
    name, error = _normalise_employee_name(raw_name)
    if error:
        return False, error, None

    existing = repository.get_employee_by_name(db, name, include_deleted=True)
    if existing is not None and existing["deleted_at"] is None:
        return False, "Сотрудник с таким ФИО уже есть в активном составе.", None
    if existing is not None:
        name = existing["name"]

    positions = []
    seen_positions = set()
    for raw_position in [*(raw_positions or []), raw_custom_position]:
        if not (raw_position or "").strip():
            continue
        position, position_error = _normalise_position(raw_position)
        if position_error:
            return False, position_error, None
        key = position.casefold()
        if key not in seen_positions:
            positions.append(position)
            seen_positions.add(key)
    if not positions:
        return False, "Выберите или введите хотя бы одну должность.", None

    chat_id = str(raw_chat_id or "").strip()
    telegram_contact = None
    if chat_id:
        telegram_contact = repository.get_telegram_contact(db, chat_id)
        if telegram_contact is None:
            return False, "Telegram-аккаунт не найден. Обновите список контактов.", None
        occupied = repository.find_link_by_chat_id(db, chat_id)
        if occupied is not None:
            return (
                False,
                f"Этот Telegram уже привязан к сотруднику {occupied['employee_name']}.",
                None,
            )

    username = _unique_login(db, name)
    password = _generate_password()
    employee_id = repository.create_employee_with_account(
        db,
        existing,
        name,
        username,
        generate_password_hash(password, method="pbkdf2:sha256"),
        positions,
        telegram_contact,
        current_timestamp(),
    )
    credentials = {
        "employee_id": employee_id,
        "employee_name": name,
        "username": username,
        "password": password,
    }
    return True, f"Сотрудник {name} добавлен. Личный кабинет создан.", credentials


def reset_employee_password(db, employee_id):
    employee = repository.get_employee(db, employee_id)
    if employee is None:
        return False, "Сотрудник не найден.", None
    account = repository.get_team_account(db, employee_id)
    password = _generate_password()
    password_hash = generate_password_hash(password, method="pbkdf2:sha256")
    if account is None:
        username = _unique_login(db, employee["name"])
        repository.create_team_account(
            db,
            employee_id,
            employee["name"],
            username,
            password_hash,
            current_timestamp(),
        )
        message = f"Личный кабинет для {employee['name']} создан."
    else:
        username = account["username"]
        repository.update_team_password(db, employee_id, password_hash)
        message = f"Новый пароль для {employee['name']} создан."
    credentials = {
        "employee_id": employee_id,
        "employee_name": employee["name"],
        "username": username,
        "password": password,
    }
    return True, message, credentials


def delete_employee(db, employee_id):
    employee = repository.get_employee(db, employee_id)
    if employee is None:
        return False, "Сотрудник не найден."
    open_assignments = repository.count_open_assignments(db, employee["name"])
    if open_assignments:
        return (
            False,
            "Нельзя удалить сотрудника: у него есть активные или ожидающие ответа "
            f"задачи ({open_assignments}). Сначала завершите или переназначьте их.",
        )

    repository.deactivate_employee(
        db,
        employee,
        current_timestamp(),
        dt.date.today().isoformat(),
    )
    return (
        True,
        f"Сотрудник {employee['name']} удалён из активного состава. История сохранена.",
    )


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


def list_candidates(db):
    candidates = []
    for row in repository.list_candidates(db):
        candidate = dict(row)
        candidate["initials"] = _initials(candidate["name"])
        candidates.append(candidate)
    return candidates


def create_candidate(db, raw_name, raw_phone, raw_note):
    name = " ".join((raw_name or "").strip().split())
    if not name:
        return False, "Укажите имя кандидата.", None
    if len(name) > CANDIDATE_NAME_MAX_LENGTH:
        return False, f"Имя должно быть короче {CANDIDATE_NAME_MAX_LENGTH + 1} символов.", None
    phone = " ".join((raw_phone or "").strip().split())
    if len(phone) > CANDIDATE_PHONE_MAX_LENGTH:
        return False, f"Телефон должен быть короче {CANDIDATE_PHONE_MAX_LENGTH + 1} символов.", None
    note = (raw_note or "").strip()
    if len(note) > CANDIDATE_NOTE_MAX_LENGTH:
        return False, f"Заметка должна быть короче {CANDIDATE_NOTE_MAX_LENGTH + 1} символов.", None

    candidate_id = repository.create_candidate(
        db, name, phone, note, current_timestamp()
    )
    return True, f"Кандидат {name} добавлен.", candidate_id


def delete_candidate(db, candidate_id):
    candidate = repository.get_candidate(db, candidate_id)
    if candidate is None:
        return False, "Кандидат не найден."
    repository.delete_candidate(db, candidate_id)
    return True, f"{candidate['name']} удалён(а) из кандидатов."


def convert_candidate(db, candidate_id, raw_positions, raw_custom_position):
    """Hand a candidate off to create_employee unchanged — same name/position
    validation and automatic login creation as adding an employee directly —
    then remove the candidate row once that succeeds. Leaves the candidate
    in place on failure (e.g. the name needs a surname first) so nothing is
    silently lost."""
    candidate = repository.get_candidate(db, candidate_id)
    if candidate is None:
        return False, "Кандидат не найден.", None

    success, message, credentials = create_employee(
        db, candidate["name"], raw_positions, raw_custom_position, ""
    )
    if not success:
        return False, message, None

    repository.delete_candidate(db, candidate_id)
    return (
        True,
        f"{candidate['name']} переведён(а) в сотрудники. Личный кабинет создан.",
        credentials,
    )


_LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9._@-]+$")


def _validate_login(raw_login):
    login = (raw_login or "").strip()
    if len(login) < EMPLOYEE_LOGIN_MIN_LENGTH or len(login) > EMPLOYEE_LOGIN_MAX_LENGTH:
        return None, (
            f"Логин должен быть от {EMPLOYEE_LOGIN_MIN_LENGTH} до "
            f"{EMPLOYEE_LOGIN_MAX_LENGTH} символов."
        )
    if not _LOGIN_PATTERN.match(login):
        return None, "Логин может содержать только латинские буквы, цифры и символы . _ - @"
    return login, None


def _validate_password(raw_password):
    password = raw_password or ""
    if len(password) < PASSWORD_MIN_LENGTH:
        return None, f"Пароль должен быть не короче {PASSWORD_MIN_LENGTH} символов."
    if len(password) > PASSWORD_MAX_LENGTH:
        return None, f"Пароль должен быть не длиннее {PASSWORD_MAX_LENGTH} символов."
    return password, None


def update_employee(db, employee_id, raw_name, raw_login, raw_password):
    """Edit an employee's ФИО, login and password. A blank login/password
    keeps the current one; a ФИО change is carried through all name-keyed
    history (see repository.rename_employee_everywhere)."""
    employee = repository.get_employee(db, employee_id)
    if employee is None:
        return False, "Сотрудник не найден."
    name, error = _normalise_employee_name(raw_name)
    if error:
        return False, error
    other = repository.get_employee_by_name(db, name, include_deleted=True)
    if other is not None and other["id"] != employee_id:
        return False, "Сотрудник с таким ФИО уже есть (в том числе среди удалённых)."

    account = repository.get_team_account(db, employee_id)
    login_raw = (raw_login or "").strip()
    password_raw = raw_password or ""
    username = None
    if login_raw:
        username, error = _validate_login(login_raw)
        if error:
            return False, error
        if username != (account["username"] if account else None) and repository.login_in_use(
            db, username, exclude_team_account_id=account["id"] if account else None
        ):
            return False, f"Логин «{username}» уже занят."
    password_hash = None
    if password_raw:
        password, error = _validate_password(password_raw)
        if error:
            return False, error
        password_hash = generate_password_hash(password, method="pbkdf2:sha256")
    if account is None and (username or password_hash) and not (username and password_hash):
        return False, "Чтобы создать личный кабинет, укажите и логин, и пароль."

    changes = []
    if name != employee["name"]:
        changes.append("ФИО")
    if account is not None and username and username != account["username"]:
        changes.append("логин")
    if password_hash:
        changes.append("пароль")
    if account is None and username and password_hash:
        changes.append("личный кабинет создан")
    if not changes:
        return True, "Изменений нет."

    repository.apply_employee_update(
        db, employee, name, account, username, password_hash, current_timestamp()
    )
    return True, f"Данные сотрудника {name} обновлены: {', '.join(changes)}."


def legacy_admins(db):
    return [
        dict(row, initials=_initials(row["admin_name"]))
        for row in repository.list_legacy_admins(db)
    ]


def update_legacy_admin(db, admin_id, raw_name, raw_login, raw_password):
    """Edit the main administrator account: displayed name, login, password."""
    admin = repository.get_legacy_admin(db, admin_id)
    if admin is None:
        return False, "Учётная запись администратора не найдена.", None
    name = " ".join((raw_name or "").strip().split())
    if not name:
        return False, "Введите имя администратора.", None
    if len(name) > ADMIN_NAME_MAX_LENGTH:
        return False, f"Имя должно быть не длиннее {ADMIN_NAME_MAX_LENGTH} символов.", None
    if any(ord(character) < 32 for character in name):
        return False, "Имя содержит недопустимые символы.", None
    login_raw = (raw_login or "").strip()
    username = admin["username"]
    if login_raw and login_raw != admin["username"]:
        username, error = _validate_login(login_raw)
        if error:
            return False, error, None
        if repository.login_in_use(db, username, exclude_admin_id=admin_id):
            return False, f"Логин «{username}» уже занят.", None
    password_hash = None
    if raw_password:
        password, error = _validate_password(raw_password)
        if error:
            return False, error, None
        password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    changes = []
    if name != admin["admin_name"]:
        changes.append("имя")
    if username != admin["username"]:
        changes.append("логин")
    if password_hash:
        changes.append("пароль")
    if not changes:
        return True, "Изменений нет.", name
    repository.update_legacy_admin(db, admin_id, name, username, password_hash)
    return True, f"Данные администратора обновлены: {', '.join(changes)}.", name

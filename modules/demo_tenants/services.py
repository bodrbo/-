import datetime as dt
import os
import re
import secrets
import string

from werkzeug.security import check_password_hash, generate_password_hash

from . import repository
from .constants import (
    DEMO_MODULE_BLUEPRINTS, DEMO_MODULE_KEYS, DEMO_MODULE_PATH_PREFIXES,
)

TENANT_NAME_MAX_LENGTH = 160
USERNAME_MAX_LENGTH = 60
PASSWORD_MIN_LENGTH = 6

# Company names here are almost always Russian — without this, every
# Cyrillic name collapses to the same empty slug ("tenant", "tenant-2", …),
# which works but makes every login/filename indistinguishable from the
# next. A simple transliteration keeps the slug (and so the username and
# database filename) actually readable.
_CYRILLIC_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _slugify(name):
    transliterated = "".join(
        _CYRILLIC_TRANSLIT.get(ch, ch) for ch in name.strip().lower()
    )
    slug = re.sub(r"[^a-z0-9]+", "-", transliterated).strip("-")
    return slug or "tenant"


def _unique_slug(db, base):
    slug = base
    n = 2
    while repository.slug_exists(db, slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


def _generate_password(length=12):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _clean_accent_color(accent_color):
    # Rendered straight into a <style> block (see _topbar_brand.html) —
    # only accept a plain hex color, never whatever was typed/submitted.
    accent_color = (accent_color or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}", accent_color):
        return ""
    return accent_color


def _validate_username(db, raw_username, exclude_id):
    username = (raw_username or "").strip()
    if not username:
        return None, "Укажите логин."
    if len(username) > USERNAME_MAX_LENGTH:
        return None, f"Логин — не более {USERNAME_MAX_LENGTH} символов."
    if not re.fullmatch(r"[A-Za-z0-9._-]+", username):
        return None, "Логин: только латинские буквы, цифры, точка, дефис и подчёркивание."
    if repository.username_exists(db, username, exclude_id=exclude_id):
        return None, "Такой логин уже занят другим демо-аккаунтом."
    return username, None


def create_tenant(
    db, raw_company_name, raw_modules, accent_color, logo_filename,
    empty_state_logo_filename, tenant_db_dir, provision_db, seed_db=None,
):
    """provision_db(path) must build the tenant's own database schema at
    that path AND make sure no real admin/investor data ends up in it
    (see _provision_demo_tenant_db in app.py). seed_db(path), if given,
    additionally populates the fresh database with demo data."""
    company_name = " ".join((raw_company_name or "").split())
    if not company_name:
        return False, "Укажите название компании.", None
    if len(company_name) > TENANT_NAME_MAX_LENGTH:
        return False, f"Название — не более {TENANT_NAME_MAX_LENGTH} символов.", None

    modules = sorted({m for m in (raw_modules or []) if m in DEMO_MODULE_KEYS})
    accent_color = _clean_accent_color(accent_color)

    slug = _unique_slug(db, _slugify(company_name))
    username = f"demo.{slug}"
    password = _generate_password()
    os.makedirs(tenant_db_dir, exist_ok=True)
    db_path = os.path.join(tenant_db_dir, f"{slug}.db")

    provision_db(db_path)
    if seed_db is not None:
        seed_db(db_path)

    tenant_id = repository.create_tenant(
        db, slug, company_name, logo_filename or None,
        empty_state_logo_filename or None, accent_color,
        ",".join(modules), username,
        generate_password_hash(password, method="pbkdf2:sha256"),
        db_path, current_timestamp(),
    )
    return True, f"Демо-аккаунт «{company_name}» создан.", {
        "tenant_id": tenant_id, "slug": slug,
        "username": username, "password": password,
    }


def update_tenant(
    db, tenant_id, raw_company_name, raw_modules, accent_color, logo_filename,
    empty_state_logo_filename, raw_username, raw_password,
):
    """logo_filename / empty_state_logo_filename are the newly uploaded
    files' names, or None each to keep whatever the tenant already has.
    raw_password blank keeps the existing password — only a non-empty
    value resets it."""
    tenant = repository.get_tenant(db, tenant_id)
    if tenant is None:
        return False, "Демо-аккаунт не найден."

    company_name = " ".join((raw_company_name or "").split())
    if not company_name:
        return False, "Укажите название компании."
    if len(company_name) > TENANT_NAME_MAX_LENGTH:
        return False, f"Название — не более {TENANT_NAME_MAX_LENGTH} символов."

    username, error = _validate_username(db, raw_username, exclude_id=tenant_id)
    if error:
        return False, error

    raw_password = (raw_password or "").strip()
    password_hash = None
    if raw_password:
        if len(raw_password) < PASSWORD_MIN_LENGTH:
            return False, f"Пароль — минимум {PASSWORD_MIN_LENGTH} символов."
        password_hash = generate_password_hash(raw_password, method="pbkdf2:sha256")

    modules = sorted({m for m in (raw_modules or []) if m in DEMO_MODULE_KEYS})
    accent_color = _clean_accent_color(accent_color)
    final_logo = logo_filename if logo_filename is not None else tenant["logo_filename"]
    final_empty_state_logo = (
        empty_state_logo_filename if empty_state_logo_filename is not None
        else tenant["empty_state_logo_filename"]
    )

    repository.update_tenant(
        db, tenant_id, company_name, final_logo, final_empty_state_logo,
        accent_color, ",".join(modules), username, password_hash,
    )
    return True, f"Демо-аккаунт «{company_name}» обновлён."


def delete_tenant(db, tenant_id):
    tenant = repository.get_tenant(db, tenant_id)
    if tenant is None:
        return False, "Демо-аккаунт не найден."
    repository.delete_tenant(db, tenant_id)
    # Best-effort — a missing/already-removed file shouldn't block the
    # account record itself from being deleted.
    try:
        if tenant["db_path"] and os.path.exists(tenant["db_path"]):
            os.remove(tenant["db_path"])
    except OSError:
        pass
    return True, f"Демо-аккаунт «{tenant['company_name']}» удалён."


def authenticate(db, username, password):
    row = repository.get_tenant_by_username(db, (username or "").strip())
    if row is None or not check_password_hash(row["password_hash"], password or ""):
        return None
    return row


def enabled_module_set(tenant_row):
    raw = tenant_row["enabled_modules"] if tenant_row else ""
    return {m for m in (raw or "").split(",") if m}


def module_for_request(blueprint_name, path):
    """Which toggleable module a request belongs to, or None if it's core
    (always allowed regardless of a tenant's enabled set)."""
    if blueprint_name:
        for module_key, blueprints in DEMO_MODULE_BLUEPRINTS.items():
            if blueprint_name in blueprints:
                return module_key
    for module_key, prefixes in DEMO_MODULE_PATH_PREFIXES.items():
        if any(path.startswith(prefix) for prefix in prefixes):
            return module_key
    return None

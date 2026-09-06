"""
Учёт выполненных работ сотрудников.

Небольшое веб-приложение на Flask + SQLite:
- форма для добавления записи (сотрудник, вид работы, часы/кол-во, ставка,
  дата работы)
- фильтр по неделям (платежи еженедельные) и по сотруднику
- выпадающий список видов работ с автоподстановкой ставки и часов
- таблица записей за выбранный период, сумма по каждому сотруднику и общая

Запуск локально:
    pip install -r requirements.txt
    python app.py
Приложение будет доступно на http://127.0.0.1:5000
"""

import os
import sys
import json
import html
import time
import uuid
import secrets
import sqlite3
import calendar
import datetime as dt
from functools import wraps
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import (
    Flask, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for,
)
from werkzeug.datastructures import MultiDict
from werkzeug.security import check_password_hash, generate_password_hash
from io import BytesIO

from modules.fleet import create_fleet_blueprint
from modules.fleet.constants import (
    BOATS,
    BOAT_COLORS,
    CHECKLIST_QUESTIONS,
    CHECKLIST_TYPE_LABELS,
    DEFECT_ASSIGNABLE_POSITIONS,
    DEFECT_STATUSES,
    DEFECT_TASK_WORK_TYPE,
    FUEL_CONFIG,
    TASK_ASSIGNMENT_COMMENT_MAX_LENGTH,
    YCLIENTS_BLOCKED_SHIFT_COLOR,
    YCLIENTS_CANCELLED_COLOR,
)
from modules.fleet import fuel_services
from modules.fleet.services import (
    add_defect_plan_item as _add_defect_plan_item,
    checklist_questions_for as _checklist_questions_for,
    create_manual_defect as _create_manual_defect,
    defect_detail_context as _defect_detail_context,
    get_checklist_answer_photos,
    save_defect_case_notes as _save_defect_case_notes,
    set_defect_plan_item_status as _set_defect_plan_item_status,
)
from integrations.telegram import fetch_recent_contacts as fetch_recent_telegram_contacts
from integrations.tripster import fetch_orders as fetch_tripster_orders
from modules.employees import create_employees_blueprint
from modules.employees.constants import (
    CUSTOMER_MANAGER_POSITION,
    EMPLOYEES,
    INITIAL_EMPLOYEE_POSITIONS,
)
from modules.employees.services import (
    active_employee_names as _active_employee_names,
    telegram_chat_id_for_employee,
)
from modules.notifications import (
    ASSIGNMENT_TUNING,
    EVENT_FLEET_CHECKLIST_PROBLEM,
    EVENT_FLEET_DEFECT_CREATED,
    EVENT_FLEET_EXTRA_DEFECT,
    EVENT_TUNING_WORK_APPROVED,
    dispatch_notification,
    dispatch_photos,
    init_schema as init_notification_schema,
    notify_task_assigned,
    send_due_task_reminders,
)
from modules.refunds import create_refunds_blueprint
from modules.refunds import services as refund_services
from modules.offline import create_offline_blueprint, init_schema as init_offline_schema
from modules.field_diagnostics import (
    create_blueprint as create_field_diagnostics_blueprint,
    init_schema as init_field_diagnostics_schema,
)
from modules.schedule import (
    create_schedule_blueprint,
    init_schema as init_schedule_schema,
)
from modules.excursion_services import (
    create_blueprint as create_excursion_services_blueprint,
    init_schema as init_excursion_services_schema,
)
from modules.software_requests import (
    create_blueprint as create_software_requests_blueprint,
    init_schema as init_software_requests_schema,
)
from modules.ai_assistant import (
    create_blueprint as create_ai_assistant_blueprint,
    init_schema as init_ai_assistant_schema,
)
from modules.ai_assistant.constants import (
    DEFAULT_MAX_CONCURRENT_REQUESTS as AI_DEFAULT_MAX_CONCURRENT_REQUESTS,
    DEFAULT_MODEL as AI_DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS as AI_DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USER_REQUESTS_PER_MINUTE as AI_DEFAULT_REQUESTS_PER_MINUTE,
)
from modules.ai_assistant.openai_client import OpenAIResponsesClient
from modules.clients import (
    ensure_segment as ensure_client_segment,
    init_schema as init_client_segments_schema,
    sync_clients as sync_yclients_clients,
)
from modules.clients.constants import (
    CLIENT_ACQUISITION_CHANNELS,
    EXCURSION_SEGMENT,
    TUNING_SEGMENT,
)
from modules.tuning_boat_specs import boat_specification_for, format_parameters

# reportlab (PDF generation for "Акт выполненных работ") is imported lazily,
# inside _build_act_pdf() — it's an extra dependency on top of the site's
# core requirements, and a missing/broken install of it must not take the
# whole app down. If it's unavailable, only that one route fails.

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # cap uploaded photos at 15 MB


@app.url_defaults
def _version_static_assets(endpoint, values):
    """Give cached static URLs a deploy-specific version automatically.

    The offline service worker intentionally caches the application shell.
    Without a changing URL an already-open browser can render fresh HTML
    with an older stylesheet after a deploy.  File mtimes change on checkout,
    while unchanged assets keep a stable, cache-friendly URL.
    """
    if endpoint != "static" or values.get("v") or not values.get("filename"):
        return
    asset_path = os.path.join(app.static_folder, values["filename"])
    try:
        values["v"] = str(os.stat(asset_path).st_mtime_ns)
    except OSError:
        pass

# Signs the investor-login session cookie. Set SECRET_KEY as a real
# environment variable in production so sessions survive restarts/deploys —
# without it, a fresh random key is generated each process start (safe,
# just logs everyone out on every restart/redeploy).
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

DB_PATH = os.environ.get("WORKHOURS_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "workhours.db"
)


def _bounded_env_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


OPENAI_AGENT_MODEL = os.environ.get("OPENAI_AGENT_MODEL", AI_DEFAULT_MODEL).strip() or AI_DEFAULT_MODEL
OPENAI_AGENT_TIMEOUT = _bounded_env_int(
    "OPENAI_AGENT_TIMEOUT", AI_DEFAULT_TIMEOUT_SECONDS, 10, 180
)
OPENAI_AGENT_MAX_CONCURRENT = _bounded_env_int(
    "OPENAI_AGENT_MAX_CONCURRENT", AI_DEFAULT_MAX_CONCURRENT_REQUESTS, 1, 20
)
OPENAI_AGENT_REQUESTS_PER_MINUTE = _bounded_env_int(
    "OPENAI_AGENT_REQUESTS_PER_MINUTE", AI_DEFAULT_REQUESTS_PER_MINUTE, 1, 60
)
_openai_responses_client = OpenAIResponsesClient(
    api_key_provider=lambda: os.environ.get("OPENAI_API_KEY", ""),
    timeout_provider=lambda: OPENAI_AGENT_TIMEOUT,
)


@app.after_request
def _no_cache_dynamic_pages(response):
    # Every page here reflects live, frequently-changing data (order
    # statuses, catalog contents, stock levels, ...). Without an explicit
    # header, a browser can serve a stale cached copy — or restore the page
    # straight from bfcache — on back/forward navigation instead of asking
    # the server again, silently showing outdated data with no error.
    # Static assets (css/js/photos) are excluded — those should still cache.
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    return response


def format_ru_date(iso_date):
    """YYYY-MM-DD -> DD/MM/YYYY for display in trip tables."""
    if not iso_date:
        return ""
    try:
        return dt.date.fromisoformat(iso_date).strftime("%d/%m/%Y")
    except ValueError:
        return iso_date


AVATAR_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def find_avatar_url(username):
    """Look for static/avatars/<username>.<ext> and return its static URL,
    or None if nobody's uploaded a photo for this person yet."""
    if not username:
        return None
    avatars_dir = os.path.join(app.static_folder, "avatars")
    for ext in AVATAR_EXTENSIONS:
        if os.path.exists(os.path.join(avatars_dir, username + ext)):
            return url_for("static", filename=f"avatars/{username}{ext}")
    return None


def find_diploma_url(username):
    """Same convention as find_avatar_url, one folder over: drop a captain's
    diploma scan at static/diplomas/<username>.<ext> and it shows up in
    their cabinet — no upload form, no DB row, nothing to build."""
    if not username:
        return None
    diplomas_dir = os.path.join(app.static_folder, "diplomas")
    for ext in AVATAR_EXTENSIONS:
        if os.path.exists(os.path.join(diplomas_dir, username + ext)):
            return url_for("static", filename=f"diplomas/{username}{ext}")
    return None


WORK_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
TUNING_BOAT_PHOTO_EXTENSIONS = WORK_PHOTO_EXTENSIONS
TUNING_BOAT_MODEL_NAME_LIMIT = 200
TUNING_BOAT_SPECIFICATIONS_LIMIT = 8000
TUNING_BOAT_SOURCE_NAME_LIMIT = 250
TUNING_BOAT_SOURCE_URL_LIMIT = 2048


def _normalize_tuning_boat_model(model_name):
    """Stable identity for one model despite case or repeated spaces."""
    return " ".join((model_name or "").split()).casefold()


def _tuning_equipment_profile_key(equipment_type, model_name):
    """Namespace model identity so a boat and motor may share a model name."""
    normalized = _normalize_tuning_boat_model(model_name)
    if not normalized:
        return ""
    return normalized if equipment_type == "boat" else f"motor:{normalized}"


def _sync_tuning_boat_profiles(db):
    """Ensure every non-empty boat or motor model has a catalog profile.

    Newest spelling wins when a model does not have a profile yet; once a
    profile exists its display name and manually entered data stay stable.
    """
    seen_keys = set()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for row in db.execute(
        "SELECT equipment_type, boat_model, motor_model FROM tuning_orders "
        "WHERE (equipment_type = 'boat' AND TRIM(boat_model) != '') "
        "OR (equipment_type = 'motor' AND TRIM(motor_model) != '') "
        "ORDER BY id DESC"
    ).fetchall():
        equipment_type = "motor" if row[0] == "motor" else "boat"
        raw_model = row[2] if equipment_type == "motor" else row[1]
        model_name = " ".join(raw_model.split())
        model_key = _tuning_equipment_profile_key(equipment_type, model_name)
        if not model_key or model_key in seen_keys:
            continue
        seen_keys.add(model_key)
        db.execute(
            "INSERT OR IGNORE INTO tuning_boat_profiles "
            "(model_key, model_name, equipment_type, specifications, "
            "created_at, updated_at) VALUES (?, ?, ?, '', ?, ?)",
            (model_key, model_name, equipment_type, now, now),
        )
    _seed_tuning_boat_profile_specifications(db)


def _seed_tuning_boat_profile_specifications(db):
    """Fill empty known profiles from the reviewed public catalog.

    Administrator-entered specifications are intentionally never replaced.
    """
    columns = {
        row[1] for row in db.execute("PRAGMA table_info(tuning_boat_profiles)")
    }
    if not {
        "specifications_source_url", "specifications_source_name"
    }.issubset(columns):
        return 0

    updated = 0
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for profile in db.execute(
        "SELECT id, model_name, specifications FROM tuning_boat_profiles "
        "WHERE equipment_type = 'boat'"
    ).fetchall():
        profile_id, model_name, specifications = profile
        if (specifications or "").strip():
            continue
        record = boat_specification_for(model_name)
        if not record:
            continue
        db.execute(
            "UPDATE tuning_boat_profiles SET specifications = ?, "
            "specifications_source_url = ?, specifications_source_name = ?, "
            "updated_at = ? WHERE id = ? AND TRIM(specifications) = ''",
            (
                format_parameters(record), record["source_url"],
                record["source_name"], now, profile_id,
            ),
        )
        updated += 1
    return updated


def _parse_boat_specifications(specifications):
    """Turn the editable ``Name: value`` format into display parameters."""
    items = []
    for raw_line in (specifications or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" in line:
            name, value = line.split(":", 1)
            items.append({"name": name.strip(), "value": value.strip()})
        else:
            items.append({"name": "Примечание", "value": line})
    return items


def _tuning_boat_photo_url(profile):
    if not profile.get("photo_filename"):
        return None
    return url_for(
        "static", filename=f"tuning_boats/{profile['photo_filename']}"
    )


def _tuning_equipment_profile_id(db, equipment_type, model_name):
    model_key = _tuning_equipment_profile_key(equipment_type, model_name)
    if not model_key:
        return None
    row = db.execute(
        "SELECT id FROM tuning_boat_profiles "
        "WHERE model_key = ? AND equipment_type = ?",
        (model_key, equipment_type),
    ).fetchone()
    return row[0] if row else None


def _tuning_boat_profile_id(db, model_name):
    return _tuning_equipment_profile_id(db, "boat", model_name)


def _tuning_boat_model_choices(db):
    """Canonical display names available to the order-form combobox."""
    return [
        row[0]
        for row in db.execute(
            "SELECT model_name FROM tuning_boat_profiles "
            "WHERE equipment_type = 'boat' "
            "ORDER BY model_name COLLATE NOCASE"
        ).fetchall()
    ]


def _rename_tuning_boat_profile(db, profile, model_name, updated_at):
    """Rename a canonical model without splitting its historical profile."""
    old_key = profile["model_key"]
    equipment_type = profile["equipment_type"]
    new_key = _tuning_equipment_profile_key(equipment_type, model_name)
    model_column = "motor_model" if equipment_type == "motor" else "boat_model"

    order_ids = []
    for row in db.execute(
        f"SELECT id, {model_column} AS model_name FROM tuning_orders "
        "WHERE equipment_type = ?",
        (equipment_type,),
    ).fetchall():
        if _tuning_equipment_profile_key(
            equipment_type, row["model_name"]
        ) == old_key:
            order_ids.append(row["id"])
    if order_ids:
        db.executemany(
            f"UPDATE tuning_orders SET {model_column} = ?, updated_at = ? WHERE id = ?",
            [(model_name, updated_at, order_id) for order_id in order_ids],
        )

    if equipment_type == "boat":
        client_ids = []
        for row in db.execute("SELECT id, boat_model FROM clients").fetchall():
            if _normalize_tuning_boat_model(row["boat_model"]) == old_key:
                client_ids.append(row["id"])
        if client_ids:
            db.executemany(
                "UPDATE clients SET boat_model = ? WHERE id = ?",
                [(model_name, client_id) for client_id in client_ids],
            )

        # Field diagnostic sheets retain a direct profile link, so their model
        # label should follow the same canonical identity as the catalog.
        db.execute(
            "UPDATE field_diagnostic_sheets SET boat_model = ? "
            "WHERE boat_profile_id = ?",
            (model_name, profile["id"]),
        )
    db.execute(
        "UPDATE tuning_boat_profiles SET model_key = ?, model_name = ?, "
        "updated_at = ? WHERE id = ?",
        (new_key, model_name, updated_at, profile["id"]),
    )
    return len(order_ids)


def _tuning_client_choices(db):
    """Clients offered by tuning workflows, excluding excursion-only people."""
    return db.execute(
        "SELECT clients.id, clients.client_name, clients.phone FROM clients "
        "WHERE EXISTS (SELECT 1 FROM client_segments "
        " WHERE client_segments.client_id = clients.id "
        " AND client_segments.segment = ?) "
        "OR NOT EXISTS (SELECT 1 FROM client_segments "
        " WHERE client_segments.client_id = clients.id) "
        "ORDER BY clients.client_name COLLATE NOCASE, clients.phone, clients.id",
        (TUNING_SEGMENT,),
    ).fetchall()


def get_work_item_photos(db, item_id):
    """All photos attached to a tuning_order_items row, oldest first, each
    with its own comment — {id, url, comment}."""
    rows = db.execute(
        "SELECT id, filename, comment FROM work_item_photos WHERE item_id = ? ORDER BY id",
        (item_id,),
    ).fetchall()
    return [
        {"id": r["id"], "url": url_for("static", filename=f"work_photos/{r['filename']}"),
         "comment": r["comment"]}
        for r in rows
    ]


def format_money(value, decimals=2):
    """Format a number with a thin space as the thousands separator and,
    when decimals > 0, a comma as the decimal separator (Russian convention
    — Python's f-string grouping only gives us a period)."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    formatted = f"{value:,.{decimals}f}"
    if "." in formatted:
        integer_part, decimal_part = formatted.split(".")
        return integer_part.replace(",", " ") + "," + decimal_part
    return formatted.replace(",", " ")


app.jinja_env.filters["ru_date"] = format_ru_date
app.jinja_env.filters["money"] = format_money

# ---------------------------------------------------------------------
# Yclients — импорт рейсов. Токены НЕ храним в коде (секреты) — задайте их
# как переменные окружения на хостинге:
#   YCLIENTS_PARTNER_TOKEN, YCLIENTS_USER_TOKEN, YCLIENTS_COMPANY_ID
# ---------------------------------------------------------------------
YCLIENTS_PARTNER_TOKEN = os.environ.get("YCLIENTS_PARTNER_TOKEN", "").strip()
YCLIENTS_USER_TOKEN = os.environ.get("YCLIENTS_USER_TOKEN", "").strip()
YCLIENTS_COMPANY_ID = os.environ.get("YCLIENTS_COMPANY_ID", "").strip()

# Tripster guide orders API. Accept the older KEY/TOKEN names as aliases so
# production can keep an already-created environment variable; no secret is
# ever stored in the repository.
TRIPSTER_API_TOKEN = (
    os.environ.get("TRIPSTER_API_TOKEN")
    or os.environ.get("TRIPSTER_API_KEY")
    or os.environ.get("TRIPSTER_TOKEN")
    or ""
).strip()

# ---------------------------------------------------------------------
# ЮKassa — онлайн-оплата тюнинг-центра и возвраты по экскурсионным рейсам.
# В отличие от
# Yclients-токенов выше, здесь НЕТ запасного значения в коде — это платёжные
# реквизиты, и в публичном репозитории им не место. Задайте на хостинге:
#   YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY
# Без них раздел оплаты в интерфейсе просто не активируется.
# ---------------------------------------------------------------------
YOOKASSA_SHOP_ID = os.environ.get("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.environ.get("YOOKASSA_SECRET_KEY")
YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"

# Магазин использует "Чеки от ЮKassa" — фискальный чек обязателен на каждый
# платёж. Ставка НДС 5% (код 7) подтверждена владельцем бизнеса — это ставка
# для АУСН/УСН по последней налоговой реформе.
YOOKASSA_RECEIPT_VAT_CODE = 7
try:
    YOOKASSA_EXCURSION_VAT_CODE = int(
        os.environ.get("YOOKASSA_EXCURSION_VAT_CODE") or YOOKASSA_RECEIPT_VAT_CODE
    )
except ValueError:
    YOOKASSA_EXCURSION_VAT_CODE = YOOKASSA_RECEIPT_VAT_CODE
YOOKASSA_EXCURSION_PAYMENT_MODE = os.environ.get(
    "YOOKASSA_EXCURSION_PAYMENT_MODE", "full_prepayment"
)
if YOOKASSA_EXCURSION_PAYMENT_MODE not in {
    "full_prepayment",
    "prepayment",
    "advance",
    "full_payment",
    "partial_payment",
    "credit",
    "credit_payment",
}:
    YOOKASSA_EXCURSION_PAYMENT_MODE = "full_prepayment"

# ---------------------------------------------------------------------
# Т-Банк — выгрузка выписки по расчётному счёту (раздел «Аналитика»).
# Как и с ЮKassa, это банковские реквизиты — никакого запасного значения в
# коде, только переменные окружения на хостинге:
#   TBANK_API_TOKEN, TBANK_ACCOUNT_NUMBER
# Без них раздел «Аналитика» просто покажет, что подключение не настроено.
#
# Выплаты самозанятым (кнопка «Отправить в Т-Банк» на «Зарплатах») требуют
# отдельного скоупа self-employed/payment-registry/manage, которого не было
# на токене для выписки — так что это отдельный токен с этим правом,
# выпущенный отдельно:
#   TBANK_API_TOKEN_PAYMENT
# Без него кнопка «Отправить в Т-Банк» просто не показывается.
# ---------------------------------------------------------------------
TBANK_API_TOKEN = os.environ.get("TBANK_API_TOKEN")
TBANK_API_TOKEN_PAYMENT = os.environ.get("TBANK_API_TOKEN_PAYMENT")
TBANK_ACCOUNT_NUMBER = os.environ.get("TBANK_ACCOUNT_NUMBER")
TBANK_API_BASE = "https://business.tbank.ru/openapi/api"

# ---------------------------------------------------------------------
# Секрет для эндпоинта, который раз в день дёргает cron на хостинге, чтобы
# проверить смены капитанов на завтра в Yclients (см. /internal/cron/...
# ниже). Тоже настраивается только через переменную окружения:
#   CRON_SECRET
# Без неё эндпоинт всегда отвечает 403 — по умолчанию выключен.
# ---------------------------------------------------------------------
CRON_SECRET = os.environ.get("CRON_SECRET")

# ---------------------------------------------------------------------
# Секрет для эндпоинта, который принимает лиды с формы обратной связи на
# сайте (Тильда → Site Settings → Forms → Webhook). Настраивается только
# через переменную окружения:
#   TILDA_WEBHOOK_SECRET
# Без неё эндпоинт всегда отвечает 403 — по умолчанию выключен. В Тильде
# укажите URL вида https://.../webhooks/tilda?token=<этот секрет>.
# ---------------------------------------------------------------------
TILDA_WEBHOOK_SECRET = os.environ.get("TILDA_WEBHOOK_SECRET")

# ---------------------------------------------------------------------
# Server-to-server webhook for leads from tuning.bodrbo.ru. The secret is
# deliberately environment-only; without it the JSON API stays unavailable.
# ---------------------------------------------------------------------
TUNING_SITE_WEBHOOK_SECRET = os.environ.get("TUNING_SITE_WEBHOOK_SECRET")

# ---------------------------------------------------------------------
# МодульКасса — автоматическая фискализация чека при записи оплаты по
# заказу вручную (см. add_tuning_payment). MODULKASSA_USERNAME/PASSWORD —
# НЕ пароль от личного кабинета МодульКассы, а логин/пароль, которые
# выдаёт их API в ответ на разовый вызов /associate (см. README/чат) —
# именно их нужно сохранить сюда.
#   MODULKASSA_USERNAME, MODULKASSA_PASSWORD  — из ответа /associate
#   MODULKASSA_ENV = "production"             — иначе (по умолчанию)
#                                                используется тестовый
#                                                контур demo.modulpos.ru,
#                                                который "фискализирует"
#                                                виртуально, без реальных
#                                                чеков
# Без логина/пароля фискализация просто тихо не выполняется — платёж всё
# равно записывается как обычно.
# ---------------------------------------------------------------------
MODULKASSA_USERNAME = os.environ.get("MODULKASSA_USERNAME")
MODULKASSA_PASSWORD = os.environ.get("MODULKASSA_PASSWORD")
MODULKASSA_BASE_URL = (
    "https://service.modulpos.ru/api/fn" if os.environ.get("MODULKASSA_ENV") == "production"
    else "https://demo.modulpos.ru/api/fn"
)
# УСН доходы минус расходы, льготная ставка НДС 5% — см. чат с заказчиком.
MODULKASSA_VAT_TAG = 1109

# ---------------------------------------------------------------------
# Telegram-бот — уведомления о некоторых событиях. Разные события могут
# идти в разные беседы:
#   TELEGRAM_BOT_TOKEN          — токен бота (общий для всех уведомлений)
#   TELEGRAM_CHAT_ID            — беседа по умолчанию (капитан сообщил о
#                                 проблеме в чек-листе)
#   TELEGRAM_APPROVAL_CHAT_ID   — беседа для «клиент согласовал работу»;
#                                 если не задана, эти уведомления тоже идут
#                                 в TELEGRAM_CHAT_ID
# Без TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID уведомления просто тихо не
# отправляются — сайт продолжает работать как обычно.
# ---------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_BOT_USERNAME = os.environ.get("TELEGRAM_BOT_USERNAME")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_APPROVAL_CHAT_ID = os.environ.get("TELEGRAM_APPROVAL_CHAT_ID") or TELEGRAM_CHAT_ID

# ---------------------------------------------------------------------
# Веб-пуши — тот же набор событий, что уходит в Telegram выше, но прямо на
# экран блокировки браузера/телефона у тех, кто нажал «Включить уведомления»
# в шапке. VAPID_PRIVATE_KEY/VAPID_PUBLIC_KEY — не сторонний секрет, а
# просто пара ключей, которую это же приложение сгенерировало для подписи
# пушей; сгенерировать новую пару можно так:
#   python3 -c "
#   from py_vapid import Vapid02
#   from cryptography.hazmat.primitives import serialization
#   import base64
#   v = Vapid02(); v.generate_keys()
#   priv = v.private_key.private_numbers().private_value.to_bytes(32, 'big')
#   pub = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
#   print('VAPID_PRIVATE_KEY=' + base64.urlsafe_b64encode(priv).decode().rstrip('='))
#   print('VAPID_PUBLIC_KEY=' + base64.urlsafe_b64encode(pub).decode().rstrip('='))
#   "
# VAPID_CLAIMS_EMAIL is a "mailto:" contact address push services may use to
# reach the sender if something's wrong — any real inbox works.
# Without all three, push notifications are just silently skipped.
# ---------------------------------------------------------------------
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL")


def _log_telegram(message):
    # Explicit stderr + flush: Passenger's error log only reliably captures
    # stderr, and without flush=True a buffered print() can sit in memory
    # and never actually reach the log file before the worker recycles.
    print(message, file=sys.stderr, flush=True)


def send_telegram_notification(text, chat_id=None):
    """Best-effort — a Telegram outage or missing config must never break
    the request that triggered the notification, so failures never raise.
    They ARE logged (see _log_telegram) so a silent failure is at least
    diagnosable after the fact, instead of vanishing entirely. Also returns
    a short status string, which callers may ignore (fire-and-forget) or
    surface directly — see /internal/telegram-test below.

    chat_id defaults to TELEGRAM_CHAT_ID — pass TELEGRAM_APPROVAL_CHAT_ID
    (or any other chat id) to route a specific event elsewhere."""
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        status = (
            "skipped: TELEGRAM_BOT_TOKEN/chat id not set "
            "(check .env was actually loaded — Passenger needs a restart after editing it)"
        )
        _log_telegram(f"Telegram notification {status}")
        return status
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.ok:
            _log_telegram("Telegram notification sent")
            return "sent"
        status = f"failed: {resp.status_code} {resp.text[:300]}"
        _log_telegram(f"Telegram notification {status}")
        return status
    except requests.RequestException as e:
        status = f"error: {e}"
        _log_telegram(f"Telegram notification {status}")
        return status


def send_telegram_photo(photo_path, caption=None, chat_id=None):
    """Same fire-and-forget contract as send_telegram_notification, for a
    photo already saved to disk. Kept as a second call per photo rather than
    a single sendMediaGroup request — simpler, and one bad photo can't take
    the rest down with it."""
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        status = "skipped: TELEGRAM_BOT_TOKEN/chat id not set"
        _log_telegram(f"Telegram photo {status}")
        return status
    try:
        with open(photo_path, "rb") as f:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data=data, files={"photo": f}, timeout=20,
            )
        if resp.ok:
            _log_telegram("Telegram photo sent")
            return "sent"
        status = f"failed: {resp.status_code} {resp.text[:300]}"
        _log_telegram(f"Telegram photo {status}")
        return status
    except (requests.RequestException, OSError) as e:
        status = f"error: {e}"
        _log_telegram(f"Telegram photo {status}")
        return status


def send_telegram_notification_to_employee(db, employee_name, text):
    """Same fire-and-forget contract as send_telegram_notification, routed
    to one employee's personal chat instead of a shared group. The employee
    directory owns this link independently from team login accounts."""
    chat_id = telegram_chat_id_for_employee(db, employee_name)
    if chat_id is None:
        status = f"skipped: no telegram_chat_id linked for {employee_name!r}"
        _log_telegram(f"Telegram notification {status}")
        return status
    return send_telegram_notification(text, chat_id=chat_id)


def _notify_task_assignment(db, assignment_type, assignment_id):
    """App adapter used by both assignment interfaces."""
    return notify_task_assigned(
        db,
        assignment_type,
        assignment_id,
        send_telegram_notification_to_employee,
    )


def send_telegram_notification_to_admin(db, admin_id, text):
    """Same fire-and-forget contract as send_telegram_notification_to_employee,
    routed to one admin's personal chat via admin_accounts.telegram_chat_id."""
    row = db.execute(
        "SELECT telegram_chat_id FROM admin_accounts WHERE id = ? AND telegram_chat_id IS NOT NULL",
        (admin_id,),
    ).fetchone()
    if row is None:
        status = f"skipped: no telegram_chat_id linked for admin_id={admin_id!r}"
        _log_telegram(f"Telegram notification {status}")
        return status
    return send_telegram_notification(text, chat_id=row["telegram_chat_id"])


def send_push_notification(title, body, role="admin", url="/"):
    """Same fire-and-forget contract as send_telegram_notification — a push
    failure must never break the request that triggered it. Sent to every
    subscription registered for `role`; a subscription the push service
    reports as gone (404/410) is pruned right away, since a browser that
    dropped it will never bring it back on its own.

    pywebpush is imported lazily (see reportlab elsewhere in this file for
    why): it's in requirements.txt, but a host that hasn't reinstalled deps
    yet should degrade to "skipped", not break the request."""
    if not (VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY and VAPID_CLAIMS_EMAIL):
        status = "skipped: VAPID keys not set"
        _log_telegram(f"Push {status}")
        return status
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        status = "skipped: pywebpush not installed"
        _log_telegram(f"Push {status}")
        return status

    db = get_db()
    subs = db.execute("SELECT * FROM push_subscriptions WHERE role = ?", (role,)).fetchall()
    if not subs:
        return "skipped: no subscriptions"

    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=json.dumps({"title": title, "body": body, "url": url}),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
                timeout=10,
            )
            sent += 1
        except WebPushException as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (404, 410):
                db.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub["id"],))
            _log_telegram(f"Push failed ({status_code}): {e}")
        except Exception as e:  # pywebpush can also raise on malformed keys, network errors, etc.
            _log_telegram(f"Push error: {e}")
    db.commit()
    return f"sent {sent}/{len(subs)}"


def tbank_statement_configured():
    return bool(TBANK_API_TOKEN and TBANK_ACCOUNT_NUMBER)


def tbank_payment_configured():
    return bool(TBANK_API_TOKEN_PAYMENT and TBANK_ACCOUNT_NUMBER)


# Красная "запись-блокер": менеджер ставит её сотруднику вместо реального
# рейса, когда его точно нельзя занимать в этот день (комментарий обычно
# "не ставить в рейсы"). Это не рейс и не смена — такую запись нужно
# полностью игнорировать: не создавать под неё карточку на подтверждение и
# не считать её поводом для доплаты за смену.
BLOCKED_SHIFT_COLOR = YCLIENTS_BLOCKED_SHIFT_COLOR
CANCELLED_TRIP_COLOR = YCLIENTS_CANCELLED_COLOR

# Виды работ со стандартной ставкой и длительностью (в часах).
# При выборе вида работы в форме ставка и часы подставятся автоматически
# (их всё равно можно будет поправить вручную перед сохранением).
WORK_TYPES = [
    {"name": "Малый тур", "rate": 1100, "hours": 1},
    {"name": "Средний тур", "rate": 1100, "hours": 1.5},
    {"name": "Большой тур", "rate": 1100, "hours": 2.5},
    {"name": "Аренда на 3 часа", "rate": 1100, "hours": 3},
    {"name": "Малый тур гид/капитан", "rate": 1870, "hours": 1},
    {"name": "Средний тур гид/капитан", "rate": 1870, "hours": 1.5},  # длительность не была указана — взял по аналогии со "Средний тур"
    {"name": "Большой тур гид-капитан", "rate": 1870, "hours": 2.5},
    {"name": "Индивидуальная аренда 1 час", "rate": 1100, "hours": 1},
    {"name": "Индивидуальная аренда на 1.5 часа", "rate": 1100, "hours": 1.5},
    {"name": "Индивидуальная аренда 2 часа", "rate": 1100, "hours": 2},
    {"name": "Индивидуальная аренда на 2.5 часа", "rate": 1100, "hours": 2.5},
]

# Гарантированный минимум за смену: если сотрудник в этот день числится в
# Yclients (есть хоть одна не удалённая запись с его именем), но его
# фактический заработок за день по нашим записям меньше этой суммы —
# добавляем доплату до неё. См. apply_minimum_shift_rate().
MIN_SHIFT_RATE = 3000
MIN_SHIFT_TOPUP_WORK_TYPE = "Доплата до минимальной ставки"

# Гонорар менеджера по продажам за неделю = оклад за отработанные смены +
# доля от общей выручки рейсов за эту неделю, тоже за отработанные смены:
#   (выручка_за_неделю * MANAGER_REVENUE_SHARE / 7 + MANAGER_BASE_SALARY / дней_в_месяце) * смены
MANAGER_REVENUE_SHARE = 0.03
MANAGER_BASE_SALARY = 45000
MANAGER_FEE_WORK_TYPE = "Гонорар менеджера по продажам"

# Соответствие ID услуги в Yclients названию "вид рейса" из WORK_TYPES выше —
# так ставка/часы при импорте подставляются автоматически. ID надёжнее
# названия (названия услуг иногда отличаются мелкими деталями — лишний
# пробел, другая формулировка — а ID услуги в Yclients не меняется).
YCLIENTS_SERVICE_ID_TO_WORK_TYPE = {
    14624788: "Малый тур",                           # Форты Кронштадта - малый тур
    14624778: "Средний тур",                         # Форты и маяки Кронштадта - средний тур
    14624702: "Большой тур",                         # Форты Кронштадта - большой тур
    14624830: "Индивидуальная аренда 1 час",
    15552422: "Индивидуальная аренда на 1.5 часа",
    14624850: "Индивидуальная аренда 2 часа",
    15916203: "Индивидуальная аренда на 2.5 часа",
    14624855: "Аренда на 3 часа",                    # Индивидуальная аренда 3 часа
}

# Резервное сопоставление по точному названию услуги — используется только
# если ID услуги не нашёлся в словаре выше.
YCLIENTS_SERVICE_TO_WORK_TYPE = {
    "Форты Кронштадта - малый тур": "Малый тур",
    "Форты и маяки Кронштадта - средний тур": "Средний тур",
    "Форты Кронштадта - большой тур": "Большой тур",
    "Индивидуальная аренда 1 час": "Индивидуальная аренда 1 час",
    "Индивидуальная аренда на 1.5 часа": "Индивидуальная аренда на 1.5 часа",
    "Индивидуальная аренда 2 часа": "Индивидуальная аренда 2 часа",
    "Индивидуальная аренда на 2.5 часа": "Индивидуальная аренда на 2.5 часа",
    "Индивидуальная аренда 3 часа": "Аренда на 3 часа",
}

CUSTOM_VALUE = "__custom__"  # спец-значение для пункта "Другое..." в списках

# Личный кабинет инвестора: (имя инвестора — должно совпадать с полем
# "investor" в BOATS, логин, хеш пароля). Хеш добавляйте через
# werkzeug.security.generate_password_hash(pwd, method="pbkdf2:sha256") —
# сам пароль в коде никогда не хранится.
# Вход в панель администратора: (имя, логин, хеш пароля). Один общий
# аккаунт — но формат такой же, как у инвесторов/команды ниже, так что при
# желании легко добавить второй именной вход.
ADMIN_ACCOUNTS = [
    ("Администратор", "admin",
     "pbkdf2:sha256:1000000$rzeinrjoYYtmGECi$204836b2c0cc8cc1fb3e7f2433aef25b443def35f6df44cf0e10caeecdf567e3"),
]

INVESTOR_ACCOUNTS = [
    ("Владимир Леонтьев", "Leontev",
     "pbkdf2:sha256:1000000$0nolAXnkfdZY8xFb$2be552efa5b2ee9263f2ddbb2029e7f2dd8b21e69b53c071a3372fc1e5edfe14"),
    ("Андрей Жаворонков", "andylark",
     "pbkdf2:sha256:1000000$E9ffeVGhqp4SxAM7$d3439a20955fef86f35307fba7dbbd47fccfafb87935afd3753866e04f041a64"),
]

# Первичные кабинеты исторически настроенных членов команды. Новые сотрудники
# и их аккаунты создаются через интерфейс «Сотрудники»; этот список остаётся
# только bootstrap-данными для новой базы и восстановления старых установок.
TEAM_ACCOUNTS = [
    ("Эльмира Бектаева", "bektaeva",
     "pbkdf2:sha256:1000000$ay2t4C4cbVLgpxPe$0c719274a55b4bd8936b23d4ccca4d1d8d92cf0f80b46334a264be20c5026144"),
    ("Дмитрий Тарусов", "tarusov",
     "pbkdf2:sha256:1000000$pPTznAeRUPyMKilS$89098918c857a400223e1d79142898ae3cd27bf014f72de90bdff5df8771ab53"),
    ("Платон Жмаев", "pzhmaev",
     "pbkdf2:sha256:1000000$dXaypGJ5lPTU12i5$f1ee256e1241f9582e4f64a6691803ad6d3375073eb2b06ac34c40300f565427"),
    ("Кирилл Бурнасов", "burnasov",
     "pbkdf2:sha256:1000000$bzS80DXwJpYbM2I9$e98576219d2b66dd0c6c755debea8c5c694c48ff789be5e4b689bdf7bf372e8e"),
    ("Андрей Жаворонков", "andylark",
     "pbkdf2:sha256:1000000$SISeExEgbIkK4mHV$39df88e3c4dd43c33ac6a7f6616505bb7c8dbef392862c3664607862cc33b5c4"),
    ("Марина Кащенко", "Kashenko",
     "pbkdf2:sha256:1000000$Jn5CnA4kS1II0z13$ee1913ce0b9ebf6d831d5c4b20e9fe1fe83dc86a952964a111c3ce2a176a069f"),
    ("Арсений Коннов", "konnov",
     "pbkdf2:sha256:1000000$K1cAmlBOoffmz5Yh$7264379b419cfdb8200d6ef478eea14f70d5861090841c94899fade88477388d"),
    ("Даниил Галецкий", "galetz",
     "pbkdf2:sha256:1000000$3oeteJt4OyqvMxkD$7b9bc3114c928b31a98f038a0990412e163b8cc5ebfd42b6fa6995a5d73d3e05"),
    ("Михаил Вишневский", "vishnevsky",
     "pbkdf2:sha256:1000000$aKQj76eKMNuDSUbs$dbf20c073db61f811964fd151c66de01ba228027a341c2c5640f54bf4f9d1333"),
]

SALE_CHANNELS = [
    {"value": "direct", "label": "Напрямую"},
    {"value": "aggregator", "label": "Через агрегатора/агента"},
    {"value": "mixed", "label": "Смешанно / другое (укажу комиссию сам)"},
]

EQUIPMENT_TYPES = [
    {"value": "boat", "label": "Лодка"},
    {"value": "motor", "label": "Мотор"},
]

ORDER_STATUSES = [
    {"value": "new_request", "label": "Новая заявка"},
    {"value": "estimate", "label": "Предварительный расчёт"},
    {"value": "in_progress", "label": "В работе"},
    {"value": "qc", "label": "Проходит независимый контроль качества"},
    {"value": "done", "label": "Выполнен"},
    {"value": "cancelled", "label": "Отменён"},
]
DEFAULT_ORDER_STATUS = "estimate"

CLIENT_STATUSES = [
    {"value": "satisfied", "label": "Довольный"},
    {"value": "neutral", "label": "Нейтральный"},
    {"value": "dissatisfied", "label": "Недовольный"},
    {"value": "blacklisted", "label": "Чёрный список"},
]
DEFAULT_CLIENT_STATUS = "neutral"
CLIENT_DIRECTORY_PAGE_SIZE = 20

WORK_STATUSES = [
    {"value": "pending", "label": "На согласовании"},
    {"value": "approved", "label": "Согласовано"},
    {"value": "in_progress", "label": "В работе"},
    {"value": "done", "label": "Выполнено"},
    {"value": "removed", "label": "Задача снята"},
]
DEFAULT_WORK_STATUS = "pending"

SUPPLY_COST_UNITS = [
    {"value": "piece", "label": "шт."},
    {"value": "sqm", "label": "м²"},
    {"value": "linear_m", "label": "пог. м"},
]
DEFAULT_SUPPLY_COST_UNIT = "piece"

SUPPLY_WRITEOFF_REASONS = ["Брак", "Недостача", "Порча при хранении", "Истёк срок годности"]

SUPPLY_PHOTO_EXTENSIONS = WORK_PHOTO_EXTENSIONS

ASSIGNMENT_STATUSES = [
    {"value": "pending", "label": "Ожидает ответа"},
    {"value": "accepted", "label": "Принята"},
    {"value": "rejected", "label": "Отклонена"},
]
# Tuning-order work items can only be handed to tuning-center staff.
TUNING_ASSIGNABLE_POSITIONS = ("Тюнингмэн",)
# supply_writeoffs.reason used when a tuningman writes off materials from
# within an assigned task — distinct from the free-choice reasons an admin
# picks in the catalog (SUPPLY_WRITEOFF_REASONS), since this one is implied
# by the write-off's origin rather than chosen.
TUNING_MATERIAL_WRITEOFF_REASON = "Использовано в работе"
# Auto-generated when adding a catalog product to a tuning order's
# "Товары" and a single warehouse has enough of it in stock.
TUNING_GOODS_WRITEOFF_REASON = "Продано в заказе"

# Captains and tuningmen can both request supply of something not on the
# shelf (special consumables, tools) — same union of positions as the
# task-assignment mechanism, since it's the same pool of field staff.
SUPPLY_REQUEST_POSITIONS = tuple(set(DEFECT_ASSIGNABLE_POSITIONS) | set(TUNING_ASSIGNABLE_POSITIONS))
SUPPLY_REQUEST_STATUSES = [
    {"value": "new", "label": "Не обработана"},
    {"value": "accepted", "label": "Принята"},
    {"value": "ordered", "label": "Заказано"},
    {"value": "shipping", "label": "В доставке"},
    {"value": "delivered", "label": "Доставлено"},
]
DEFAULT_SUPPLY_REQUEST_STATUS = "new"
# supply_requests.employee_name used for requests raised automatically by
# _maybe_create_low_stock_request rather than by a person — distinguishes
# them in the admin table, and deliberately never matches a real
# team_accounts row so they can't accidentally trigger a Telegram DM or
# show up in anyone's personal "Мои задачи".
SUPPLY_LOW_STOCK_REQUESTER = "Автозаявка: минимальный остаток"


def order_status_label(value):
    for s in ORDER_STATUSES:
        if s["value"] == value:
            return s["label"]
    return value


def tuning_equipment_label(order):
    """Human-readable equipment identity for templates, PDFs and notices.

    Missing new columns are tolerated for older test fixtures and utility
    callers that still pass a minimal dict containing only boat_model.
    """
    try:
        equipment_type = order["equipment_type"] or "boat"
    except (KeyError, IndexError):
        equipment_type = "boat"
    try:
        boat_model = (order["boat_model"] or "").strip()
    except (KeyError, IndexError):
        boat_model = ""
    try:
        motor_model = (order["motor_model"] or "").strip()
    except (KeyError, IndexError):
        motor_model = ""
    try:
        boat_registration_number = (order["boat_registration_number"] or "").strip()
    except (KeyError, IndexError):
        boat_registration_number = ""
    try:
        motor_serial_number = (order["motor_serial_number"] or "").strip()
    except (KeyError, IndexError):
        motor_serial_number = ""

    if equipment_type == "motor":
        label = motor_model or "Мотор"
        if motor_serial_number:
            label += f" · серийный № {motor_serial_number}"
        return label

    label = boat_model or "Лодка"
    if boat_registration_number:
        label += f" · бортовой № {boat_registration_number}"
    if motor_model:
        label += f" · мотор {motor_model}"
    return label


app.add_template_filter(tuning_equipment_label, "tuning_equipment_label")


def work_status_label(value):
    for s in WORK_STATUSES:
        if s["value"] == value:
            return s["label"]
    return value


def mk_status_label(value):
    return MODULKASSA_STATUS_DISPLAY.get(value, (value, "pending"))[0]


def mk_status_css(value):
    return MODULKASSA_STATUS_DISPLAY.get(value, (value, "pending"))[1]


app.jinja_env.filters["order_status_label"] = order_status_label
app.jinja_env.filters["work_status_label"] = work_status_label
app.jinja_env.filters["mk_status_label"] = mk_status_label
app.jinja_env.filters["mk_status_css"] = mk_status_css

YOOKASSA_STATUS_LABELS = {
    "pending": "Ожидает оплаты",
    "waiting_for_capture": "Обрабатывается",
    "succeeded": "Оплачено",
    "canceled": "Отменён",
}


def yookassa_status_label(value):
    return YOOKASSA_STATUS_LABELS.get(value, value)


app.jinja_env.filters["yookassa_status_label"] = yookassa_status_label

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]

MONTHS_NOM = [
    "январь", "февраль", "март", "апрель", "май", "июнь",
    "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.create_function(
            "CASEFOLD", 1, lambda value: str(value or "").casefold()
        )
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create the entries table if needed, and migrate older DBs that don't
    yet have the work_date column."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee TEXT NOT NULL,
            work_type TEXT NOT NULL,
            rate REAL NOT NULL,
            quantity REAL NOT NULL,
            amount REAL NOT NULL,
            work_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration path for databases created before work_date existed.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()]
    if "work_date" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN work_date TEXT")
        conn.execute(
            "UPDATE entries SET work_date = substr(created_at, 1, 10) "
            "WHERE work_date IS NULL"
        )
    if "project_id" not in cols:
        conn.execute("ALTER TABLE entries ADD COLUMN project_id INTEGER")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            deleted_at TEXT
        )
        """
    )
    employee_cols = [row[1] for row in conn.execute("PRAGMA table_info(employees)").fetchall()]
    if "deleted_at" not in employee_cols:
        conn.execute("ALTER TABLE employees ADD COLUMN deleted_at TEXT")
    employee_positions_is_new = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'employee_positions'"
    ).fetchone() is None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            position TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(employee_id, position)
        )
        """
    )
    # Backfill a row for every employee already known to the system — the
    # configured EMPLOYEES list plus any name used in entries but not in it
    # (mirrors the "known" merge in _payroll_context so nobody is missed).
    known_employee_names = list(EMPLOYEES)
    for row in conn.execute("SELECT DISTINCT employee FROM entries").fetchall():
        if row[0] not in known_employee_names:
            known_employee_names.append(row[0])
    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for name in known_employee_names:
        conn.execute(
            "INSERT OR IGNORE INTO employees (name, created_at) VALUES (?, ?)",
            (name, now_str),
        )
    # Positions become runtime data once the table exists: the administrator
    # interface is the source of truth, so a restart must never undo its
    # changes. Bootstrap defaults are only inserted for a brand-new database.
    if employee_positions_is_new:
        for name, positions in INITIAL_EMPLOYEE_POSITIONS.items():
            employee_row = conn.execute(
                "SELECT id FROM employees WHERE name = ?", (name,)
            ).fetchone()
            if employee_row is None:
                continue
            for position in positions:
                conn.execute(
                    "INSERT OR IGNORE INTO employee_positions "
                    "(employee_id, position, created_at) VALUES (?, ?, ?)",
                    (employee_row[0], position, now_str),
                )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS captain_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL,
            shift_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(employee_id, shift_date)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boat_checklists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT NOT NULL,
            checklist_type TEXT NOT NULL,
            boat TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boat_checklist_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            checklist_id INTEGER NOT NULL,
            question_index INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            status TEXT NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(checklist_id, question_index)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checklist_answer_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            answer_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boat_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boat TEXT NOT NULL,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            original_filename TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fleet_boat_profiles (
            boat TEXT PRIMARY KEY,
            photo_filename TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    boat_defects_is_new = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'boat_defects'"
    ).fetchone() is None
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boat_defects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boat TEXT NOT NULL,
            checklist_id INTEGER,
            answer_id INTEGER,
            description TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            reported_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    defect_cols = [row[1] for row in conn.execute("PRAGMA table_info(boat_defects)").fetchall()]
    if "anamnesis" not in defect_cols:
        conn.execute("ALTER TABLE boat_defects ADD COLUMN anamnesis TEXT NOT NULL DEFAULT ''")
    if "diagnosis" not in defect_cols:
        conn.execute("ALTER TABLE boat_defects ADD COLUMN diagnosis TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boat_defect_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            defect_id INTEGER NOT NULL,
            source_boat TEXT NOT NULL,
            destination_boat TEXT NOT NULL,
            transferred_by TEXT NOT NULL,
            transferred_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_boat_defect_transfers_defect "
        "ON boat_defect_transfers(defect_id, transferred_at)"
    )
    if boat_defects_is_new:
        # One-time backfill: every "problem" answer ever reported through a
        # standard checklist question becomes a tracked defect too, so
        # nothing captains already flagged is missing from the new table.
        # Plain tuples here, not sqlite3.Row — this connection (unlike
        # get_db()'s) never sets row_factory, so columns are positional.
        now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        for answer_id, question_text, comment, created_at, boat, checklist_id, employee_name in conn.execute(
            "SELECT a.id, a.question_text, a.comment, a.created_at, "
            "c.boat, c.id, c.employee_name "
            "FROM boat_checklist_answers a JOIN boat_checklists c ON c.id = a.checklist_id "
            "WHERE a.status = 'problem'"
        ).fetchall():
            description = f"{question_text} — {comment}" if comment else question_text
            conn.execute(
                "INSERT INTO boat_defects (boat, checklist_id, answer_id, description, employee_name, "
                "status, reported_at, updated_at) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)",
                (boat, checklist_id, answer_id, description, employee_name, created_at, now_str),
            )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS defect_work_plan_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            defect_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            endpoint TEXT NOT NULL UNIQUE,
            p256dh TEXT NOT NULL,
            auth TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS defect_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            defect_id INTEGER NOT NULL,
            employee_name TEXT NOT NULL,
            rate REAL NOT NULL,
            norm_hours REAL NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            assignment_status TEXT NOT NULL DEFAULT 'pending',
            assigned_at TEXT NOT NULL,
            responded_at TEXT,
            entry_id INTEGER
        )
        """
    )
    defect_assignment_cols = [
        row[1] for row in conn.execute("PRAGMA table_info(defect_assignments)").fetchall()
    ]
    if "comment" not in defect_assignment_cols:
        conn.execute(
            "ALTER TABLE defect_assignments "
            "ADD COLUMN comment TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_item_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            employee_name TEXT NOT NULL,
            rate REAL NOT NULL,
            norm_hours REAL NOT NULL,
            comment TEXT NOT NULL DEFAULT '',
            assignment_status TEXT NOT NULL DEFAULT 'pending',
            assigned_at TEXT NOT NULL,
            responded_at TEXT,
            entry_id INTEGER
        )
        """
    )
    tuning_assignment_cols = [
        row[1]
        for row in conn.execute("PRAGMA table_info(tuning_item_assignments)").fetchall()
    ]
    if "comment" not in tuning_assignment_cols:
        conn.execute(
            "ALTER TABLE tuning_item_assignments "
            "ADD COLUMN comment TEXT NOT NULL DEFAULT ''"
        )
    init_notification_schema(conn)
    init_excursion_services_schema(conn)
    init_schedule_schema(conn)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boat_fuel_state (
            boat TEXT PRIMARY KEY,
            activated_at TEXT,
            activated_by_role TEXT,
            activated_by_name TEXT,
            last_synced_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boat_fuel_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boat TEXT NOT NULL,
            kind TEXT NOT NULL,
            liters_delta REAL NOT NULL,
            reserve_delta REAL NOT NULL DEFAULT 0,
            reported_liters REAL,
            occurred_at TEXT NOT NULL,
            source_ref TEXT NOT NULL UNIQUE,
            source_label TEXT,
            created_by_role TEXT NOT NULL,
            created_by_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            deleted_by TEXT
        )
        """
    )
    fuel_transaction_cols = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(boat_fuel_transactions)"
        ).fetchall()
    ]
    if "deleted_at" not in fuel_transaction_cols:
        conn.execute(
            "ALTER TABLE boat_fuel_transactions ADD COLUMN deleted_at TEXT"
        )
    if "deleted_by" not in fuel_transaction_cols:
        conn.execute(
            "ALTER TABLE boat_fuel_transactions ADD COLUMN deleted_by TEXT"
        )
    if "reserve_delta" not in fuel_transaction_cols:
        conn.execute(
            "ALTER TABLE boat_fuel_transactions "
            "ADD COLUMN reserve_delta REAL NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS boat_fuel_trip_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_ref TEXT NOT NULL UNIQUE,
            boat TEXT NOT NULL,
            trip_kind TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL,
            service_title TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            consumption_liters REAL,
            transaction_id INTEGER,
            last_seen_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_boat_fuel_transactions_boat_time "
        "ON boat_fuel_transactions (boat, occurred_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_boat_fuel_transactions_active_time "
        "ON boat_fuel_transactions (boat, deleted_at, occurred_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_boat_fuel_trip_events_boat_status "
        "ON boat_fuel_trip_events (boat, status, ended_at)"
    )
    for boat_name in FUEL_CONFIG:
        conn.execute(
            "INSERT OR IGNORE INTO boat_fuel_state (boat, updated_at) VALUES (?, ?)",
            (boat_name, now_str),
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boat TEXT NOT NULL,
            trip_date TEXT NOT NULL,
            work_type TEXT NOT NULL,
            entry_id INTEGER,
            revenue REAL NOT NULL,
            sale_channel TEXT NOT NULL,
            commission_pct REAL NOT NULL,
            commission_amount REAL NOT NULL,
            labor_cost REAL NOT NULL,
            fuel_cost REAL NOT NULL,
            mooring_cost REAL NOT NULL,
            extra_total REAL NOT NULL,
            remainder REAL NOT NULL,
            investor_payout REAL NOT NULL,
            my_share REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration path for databases created before trip_time existed.
    trip_cols = [row[1] for row in conn.execute("PRAGMA table_info(trips)").fetchall()]
    if "trip_time" not in trip_cols:
        conn.execute("ALTER TABLE trips ADD COLUMN trip_time TEXT")
        conn.execute("UPDATE trips SET trip_time = '00:00' WHERE trip_time IS NULL")
    if "is_expense" not in trip_cols:
        conn.execute("ALTER TABLE trips ADD COLUMN is_expense INTEGER NOT NULL DEFAULT 0")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_number TEXT NOT NULL,
            contract_date TEXT NOT NULL,
            client_name TEXT NOT NULL,
            client_representative TEXT,
            client_representative_basis TEXT,
            client_requisites TEXT,
            service_description TEXT NOT NULL,
            service_date TEXT,
            service_time TEXT,
            total_amount REAL NOT NULL,
            prepayment_terms TEXT,
            prepayment_amount REAL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_labor (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            entry_id INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS yclients_imports (
            yclients_ref TEXT PRIMARY KEY,
            trip_id INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS yclients_sync_state (
            sync_key TEXT PRIMARY KEY,
            last_success_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS import_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yclients_ref TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee TEXT NOT NULL,
            period_key TEXT NOT NULL,
            paid_at TEXT NOT NULL,
            UNIQUE(employee, period_key)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            equipment_type TEXT NOT NULL DEFAULT 'boat',
            boat_model TEXT NOT NULL,
            boat_registration_number TEXT NOT NULL DEFAULT '',
            motor_model TEXT NOT NULL DEFAULT '',
            motor_serial_number TEXT NOT NULL DEFAULT '',
            sale_channel TEXT NOT NULL,
            phone TEXT NOT NULL,
            discount_pct REAL NOT NULL DEFAULT 0,
            subtotal REAL NOT NULL,
            total REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'estimate',
            order_date TEXT NOT NULL DEFAULT CURRENT_DATE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_boat_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_key TEXT NOT NULL UNIQUE,
            model_name TEXT NOT NULL,
            equipment_type TEXT NOT NULL DEFAULT 'boat',
            photo_filename TEXT,
            specifications TEXT NOT NULL DEFAULT '',
            specifications_source_url TEXT NOT NULL DEFAULT '',
            specifications_source_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            work_name TEXT NOT NULL,
            cost_price REAL NOT NULL,
            multiplier REAL NOT NULL,
            price REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_order_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            quantity REAL NOT NULL,
            unit_price REAL NOT NULL,
            cost_price REAL NOT NULL,
            unit TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_order_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            author_admin_id INTEGER,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_order_note_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            note_id INTEGER NOT NULL,
            remind_admin_id INTEGER,
            remind_employee_id INTEGER,
            remind_at TEXT NOT NULL,
            sent_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    note_reminder_cols = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(tuning_order_note_reminders)"
        ).fetchall()
    }
    if "remind_employee_id" not in note_reminder_cols:
        conn.execute(
            "ALTER TABLE tuning_order_note_reminders "
            "ADD COLUMN remind_employee_id INTEGER"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tuning_note_reminders_employee "
        "ON tuning_order_note_reminders (remind_employee_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            paid_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration path for databases created before payments counted as
    # project income (for Analytics) — the backfill (populating this for
    # already-existing payments) runs further down, after the projects
    # table exists.
    tuning_payment_cols = [row[1] for row in conn.execute("PRAGMA table_info(tuning_payments)").fetchall()]
    if "project_id" not in tuning_payment_cols:
        conn.execute("ALTER TABLE tuning_payments ADD COLUMN project_id INTEGER")
    if "payment_type" not in tuning_payment_cols:
        # CASH or CARD — how the payment was actually received, matching
        # ModulKassa's own moneyPositions.paymentType values directly (no
        # separate internal enum) since it's only ever used to fill that
        # field when fiscalizing. NULL for payments recorded before this
        # column existed, or wherever the admin skipped picking one.
        conn.execute("ALTER TABLE tuning_payments ADD COLUMN payment_type TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS modulkassa_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL,
            doc_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'queued',
            fiscal_info_json TEXT,
            failure_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tuning_yookassa_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            yookassa_payment_id TEXT NOT NULL UNIQUE,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            confirmation_url TEXT NOT NULL,
            tuning_payment_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS excursion_refund_records (
            yclients_record_id INTEGER PRIMARY KEY,
            activity_id INTEGER,
            visit_id INTEGER,
            trip_at TEXT NOT NULL,
            service_title TEXT NOT NULL,
            client_name TEXT,
            client_phone TEXT,
            client_email TEXT,
            expected_amount REAL NOT NULL DEFAULT 0,
            paid_full INTEGER NOT NULL DEFAULT 0,
            prepaid INTEGER NOT NULL DEFAULT 0,
            prepaid_confirmed INTEGER NOT NULL DEFAULT 0,
            is_online INTEGER NOT NULL DEFAULT 0,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL,
            last_synced_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS excursion_yookassa_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            yookassa_payment_id TEXT NOT NULL UNIQUE,
            yclients_record_id INTEGER,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'RUB',
            refunded_amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            refundable INTEGER NOT NULL DEFAULT 0,
            description TEXT,
            payment_method TEXT,
            card_last4 TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            remote_created_at TEXT NOT NULL,
            link_method TEXT,
            linked_by TEXT,
            linked_at TEXT,
            last_synced_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS excursion_refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL,
            yookassa_refund_id TEXT UNIQUE,
            amount REAL NOT NULL,
            status TEXT NOT NULL,
            refund_kind TEXT NOT NULL,
            reason TEXT NOT NULL,
            receipt_email TEXT,
            refunded_before REAL NOT NULL DEFAULT 0,
            receipt_registration TEXT,
            cancellation_reason TEXT,
            error_message TEXT,
            idempotence_key TEXT NOT NULL UNIQUE,
            request_json TEXT NOT NULL,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    excursion_refund_cols = [
        row[1]
        for row in conn.execute("PRAGMA table_info(excursion_refunds)").fetchall()
    ]
    if "refunded_before" not in excursion_refund_cols:
        conn.execute(
            "ALTER TABLE excursion_refunds "
            "ADD COLUMN refunded_before REAL NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_excursion_refund_records_trip_at "
        "ON excursion_refund_records (trip_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_excursion_yookassa_record "
        "ON excursion_yookassa_payments (yclients_record_id, remote_created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_excursion_refunds_payment "
        "ON excursion_refunds (payment_id, created_at)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            boat_model TEXT NOT NULL DEFAULT '',
            phone TEXT DEFAULT '',
            token TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'neutral',
            created_at TEXT NOT NULL,
            yclients_client_id INTEGER,
            email TEXT NOT NULL DEFAULT '',
            birth_date TEXT NOT NULL DEFAULT '',
            comment TEXT NOT NULL DEFAULT '',
            acquisition_channel TEXT NOT NULL DEFAULT '',
            yclients_last_change_date TEXT NOT NULL DEFAULT ''
        )
        """
    )
    client_cols = [row[1] for row in conn.execute("PRAGMA table_info(clients)").fetchall()]
    if "status" not in client_cols:
        conn.execute(
            "ALTER TABLE clients ADD COLUMN status TEXT NOT NULL DEFAULT 'neutral'"
        )
    if "acquisition_channel" not in client_cols:
        conn.execute(
            "ALTER TABLE clients "
            "ADD COLUMN acquisition_channel TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hull_diagnostic_sheets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            boat_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    sheet_cols = [row[1] for row in conn.execute("PRAGMA table_info(hull_diagnostic_sheets)").fetchall()]
    if "tuning_order_id" not in sheet_cols:
        conn.execute("ALTER TABLE hull_diagnostic_sheets ADD COLUMN tuning_order_id INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hull_diagnostic_defects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id INTEGER NOT NULL,
            view TEXT NOT NULL,
            x_pct REAL NOT NULL,
            y_pct REAL NOT NULL,
            defect_type TEXT NOT NULL,
            defect_size TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    init_field_diagnostics_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bank_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id TEXT NOT NULL UNIQUE,
            account_number TEXT NOT NULL,
            operation_date TEXT NOT NULL,
            amount REAL NOT NULL,
            direction TEXT NOT NULL,
            counterparty_name TEXT,
            counterparty_inn TEXT,
            purpose TEXT,
            category TEXT,
            status TEXT,
            raw_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    bank_tx_cols = [row[1] for row in conn.execute("PRAGMA table_info(bank_transactions)").fetchall()]
    if "project_id" not in bank_tx_cols:
        conn.execute("ALTER TABLE bank_transactions ADD COLUMN project_id INTEGER")
    if "item_id" not in bank_tx_cols:
        # Optional finer-grained attribution than project_id alone — links
        # to a specific tuning_order_items row (a single work item within
        # the project's order) so Analytics can show which individual jobs
        # are profitable, not just whole orders. NULL means "whole
        # project", same as before this column existed.
        conn.execute("ALTER TABLE bank_transactions ADD COLUMN item_id INTEGER")
    if "source" not in bank_tx_cols:
        # 'tbank' (the default, so every row imported before this column
        # existed is correctly backfilled) or 'manual' — a cash payment or
        # anything else that never hits the bank statement. Distinguishes
        # rows an admin typed in by hand (editable/deletable) from imported
        # bank data (should stay in sync with the statement, not be
        # hand-edited or deleted here).
        conn.execute("ALTER TABLE bank_transactions ADD COLUMN source TEXT NOT NULL DEFAULT 'tbank'")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tbank_payout_registries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee TEXT NOT NULL,
            period_key TEXT NOT NULL,
            amount REAL NOT NULL,
            recipient_name TEXT,
            recipient_inn TEXT,
            payment_registry_id TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supply_warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supply_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            sku TEXT,
            description TEXT,
            supplier TEXT,
            photo_filename TEXT,
            cost_price REAL NOT NULL,
            cost_unit TEXT NOT NULL,
            sale_price REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration path for databases created before the low-stock auto-request
    # feature existed. NULL/0 means "no threshold set" — the check is
    # simply skipped for that product.
    supply_product_cols = [row[1] for row in conn.execute("PRAGMA table_info(supply_products)").fetchall()]
    if "min_stock" not in supply_product_cols:
        conn.execute("ALTER TABLE supply_products ADD COLUMN min_stock REAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supply_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            quantity REAL NOT NULL DEFAULT 0,
            zone TEXT,
            rack TEXT,
            spot TEXT,
            UNIQUE(product_id, warehouse_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supply_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            zone TEXT,
            rack TEXT,
            spot TEXT,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supply_writeoffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            quantity REAL NOT NULL,
            reason TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration path for databases created before task-linked write-offs
    # existed — lets a write-off be attributed to a project (for Analytics)
    # and, when it came from an employee's assigned task rather than the
    # admin panel, to who did it and which task it was for.
    supply_writeoff_cols = [row[1] for row in conn.execute("PRAGMA table_info(supply_writeoffs)").fetchall()]
    if "project_id" not in supply_writeoff_cols:
        conn.execute("ALTER TABLE supply_writeoffs ADD COLUMN project_id INTEGER")
    if "cost_price" not in supply_writeoff_cols:
        conn.execute("ALTER TABLE supply_writeoffs ADD COLUMN cost_price REAL")
    if "amount" not in supply_writeoff_cols:
        conn.execute("ALTER TABLE supply_writeoffs ADD COLUMN amount REAL")
    if "employee_name" not in supply_writeoff_cols:
        conn.execute("ALTER TABLE supply_writeoffs ADD COLUMN employee_name TEXT")
    if "tuning_item_assignment_id" not in supply_writeoff_cols:
        conn.execute("ALTER TABLE supply_writeoffs ADD COLUMN tuning_item_assignment_id INTEGER")
    # Migration path for databases created before adding a catalog product
    # to a tuning order (the "Товары" section) auto-wrote it off stock —
    # links back to the specific tuning_order_products row so removing
    # that row can find and reverse its write-off.
    if "tuning_order_product_id" not in supply_writeoff_cols:
        conn.execute("ALTER TABLE supply_writeoffs ADD COLUMN tuning_order_product_id INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supply_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            status_comment TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )
    # Migration path for databases created before low-stock auto-requests
    # existed. Set only on system-generated requests (see
    # _maybe_create_low_stock_request) — lets the check find "is there
    # already an open request for this product" so a product sitting below
    # threshold doesn't spawn a fresh one on every write-off.
    supply_request_cols = [row[1] for row in conn.execute("PRAGMA table_info(supply_requests)").fetchall()]
    if "product_id" not in supply_request_cols:
        conn.execute("ALTER TABLE supply_requests ADD COLUMN product_id INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supply_request_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            quantity REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            tuning_order_id INTEGER,
            created_at TEXT NOT NULL
        )
        """
    )
    # Backfill: every tuning order should have a matching project, including
    # ones created before this feature existed.
    conn.execute(
        """
        INSERT INTO projects (name, tuning_order_id, created_at)
        SELECT 'Заказ №' || tuning_orders.id, tuning_orders.id, tuning_orders.created_at
        FROM tuning_orders
        WHERE tuning_orders.id NOT IN (
            SELECT tuning_order_id FROM projects WHERE tuning_order_id IS NOT NULL
        )
        """
    )
    # Backfill tuning_payments.project_id now that every order has a
    # project — so payments recorded before this feature existed also
    # count as income in Analytics, not just new ones.
    conn.execute(
        """
        UPDATE tuning_payments SET project_id = (
            SELECT projects.id FROM projects WHERE projects.tuning_order_id = tuning_payments.order_id
        )
        WHERE project_id IS NULL
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS transaction_splits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transaction_id INTEGER NOT NULL,
            project_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    transaction_split_cols = [row[1] for row in conn.execute("PRAGMA table_info(transaction_splits)").fetchall()]
    if "item_id" not in transaction_split_cols:
        # Same optional per-work-item attribution as bank_transactions.item_id.
        conn.execute("ALTER TABLE transaction_splits ADD COLUMN item_id INTEGER")
    # Migration path for tuning_orders created before client_id/status existed.
    tuning_cols = [row[1] for row in conn.execute("PRAGMA table_info(tuning_orders)").fetchall()]
    if "client_id" not in tuning_cols:
        conn.execute("ALTER TABLE tuning_orders ADD COLUMN client_id INTEGER")
    if "status" not in tuning_cols:
        conn.execute(
            "ALTER TABLE tuning_orders ADD COLUMN status TEXT NOT NULL DEFAULT 'estimate'"
        )
    if "discount_type" not in tuning_cols:
        conn.execute("ALTER TABLE tuning_orders ADD COLUMN discount_type TEXT NOT NULL DEFAULT 'percent'")
        conn.execute("ALTER TABLE tuning_orders ADD COLUMN discount_value REAL NOT NULL DEFAULT 0")
        # discount_pct is the only place the old discount lived — carry it
        # over once so existing orders keep showing the same discount.
        conn.execute("UPDATE tuning_orders SET discount_value = discount_pct")
    if "source" not in tuning_cols:
        # 'manual' (default, backfilling every pre-existing order) or
        # 'tilda' — a lead that came in through the site's feedback form
        # webhook rather than an admin typing it in. source_ref carries
        # Tilda's own tranid for that submission, so a retried webhook
        # delivery (Tilda resends up to twice if it doesn't get a 200 back
        # in time) can be recognized and skipped instead of creating a
        # duplicate order.
        conn.execute("ALTER TABLE tuning_orders ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        conn.execute("ALTER TABLE tuning_orders ADD COLUMN source_ref TEXT")
    if "equipment_type" not in tuning_cols:
        conn.execute(
            "ALTER TABLE tuning_orders "
            "ADD COLUMN equipment_type TEXT NOT NULL DEFAULT 'boat'"
        )
    if "motor_model" not in tuning_cols:
        conn.execute(
            "ALTER TABLE tuning_orders "
            "ADD COLUMN motor_model TEXT NOT NULL DEFAULT ''"
        )
    if "boat_registration_number" not in tuning_cols:
        conn.execute(
            "ALTER TABLE tuning_orders "
            "ADD COLUMN boat_registration_number TEXT NOT NULL DEFAULT ''"
        )
    if "motor_serial_number" not in tuning_cols:
        conn.execute(
            "ALTER TABLE tuning_orders "
            "ADD COLUMN motor_serial_number TEXT NOT NULL DEFAULT ''"
        )
    if "order_date" not in tuning_cols:
        # Business date shown in the interface.  It deliberately remains
        # separate from created_at: migrated orders may have been inserted
        # into Bodry Business months after they were originally accepted.
        conn.execute(
            "ALTER TABLE tuning_orders "
            "ADD COLUMN order_date TEXT NOT NULL DEFAULT ''"
        )
    conn.execute(
        "UPDATE tuning_orders SET order_date = CASE "
        "WHEN created_at GLOB '????-??-??*' THEN substr(created_at, 1, 10) "
        "WHEN created_at GLOB '??.??.????*' THEN "
        "substr(created_at, 7, 4) || '-' || substr(created_at, 4, 2) || '-' || substr(created_at, 1, 2) "
        "ELSE date('now') END "
        "WHERE order_date IS NULL OR TRIM(order_date) = ''"
    )
    # Databases upgraded through ALTER TABLE keep an empty-string default.
    # The trigger protects integrations or maintenance scripts that omit the
    # new field, while explicit form/import dates remain untouched.
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS set_tuning_order_date_after_insert "
        "AFTER INSERT ON tuning_orders "
        "WHEN NEW.order_date IS NULL OR TRIM(NEW.order_date) = '' "
        "BEGIN UPDATE tuning_orders SET order_date = CASE "
        "WHEN NEW.created_at GLOB '????-??-??*' THEN substr(NEW.created_at, 1, 10) "
        "WHEN NEW.created_at GLOB '??.??.????*' THEN "
        "substr(NEW.created_at, 7, 4) || '-' || substr(NEW.created_at, 4, 2) || '-' || substr(NEW.created_at, 1, 2) "
        "ELSE date('now') END WHERE id = NEW.id; END"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tuning_orders_order_date "
        "ON tuning_orders (order_date)"
    )
    boat_profile_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(tuning_boat_profiles)").fetchall()
    }
    if "specifications_source_url" not in boat_profile_cols:
        conn.execute(
            "ALTER TABLE tuning_boat_profiles ADD COLUMN "
            "specifications_source_url TEXT NOT NULL DEFAULT ''"
        )
    if "specifications_source_name" not in boat_profile_cols:
        conn.execute(
            "ALTER TABLE tuning_boat_profiles ADD COLUMN "
            "specifications_source_name TEXT NOT NULL DEFAULT ''"
        )
    if "equipment_type" not in boat_profile_cols:
        conn.execute(
            "ALTER TABLE tuning_boat_profiles ADD COLUMN "
            "equipment_type TEXT NOT NULL DEFAULT 'boat'"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tuning_equipment_profiles_type_name "
        "ON tuning_boat_profiles (equipment_type, model_name)"
    )
    _sync_tuning_boat_profiles(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tilda_webhook_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            received_at TEXT NOT NULL,
            token_ok INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            result TEXT NOT NULL
        )
        """
    )
    item_cols = [row[1] for row in conn.execute("PRAGMA table_info(tuning_order_items)").fetchall()]
    if "status" not in item_cols:
        conn.execute(
            "ALTER TABLE tuning_order_items ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'"
        )
    if "photo_comment" not in item_cols:
        conn.execute("ALTER TABLE tuning_order_items ADD COLUMN photo_comment TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS work_item_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    # One-time carryover: the very first version of this feature stored a
    # single photo per item as static/work_photos/<item_id>.<ext>, with its
    # comment on tuning_order_items.photo_comment. Fold any such file still
    # sitting there into the new multi-photo table so nothing gets lost.
    legacy_photos_dir = os.path.join(app.static_folder, "work_photos")
    if os.path.isdir(legacy_photos_dir):
        for item_row in conn.execute("SELECT id, photo_comment FROM tuning_order_items").fetchall():
            item_id = item_row[0]
            already_migrated = conn.execute(
                "SELECT 1 FROM work_item_photos WHERE item_id = ?", (item_id,)
            ).fetchone()
            if already_migrated:
                continue
            for ext in WORK_PHOTO_EXTENSIONS:
                legacy_path = os.path.join(legacy_photos_dir, f"{item_id}{ext}")
                if os.path.exists(legacy_path):
                    new_filename = f"{item_id}-{secrets.token_hex(6)}{ext}"
                    os.rename(legacy_path, os.path.join(legacy_photos_dir, new_filename))
                    conn.execute(
                        "INSERT INTO work_item_photos (item_id, filename, comment, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (item_id, new_filename, item_row[1],
                         dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
                    )
                    break
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration path for databases created before order-note reminders (and
    # per-admin Telegram notifications generally) existed. Administrator
    # accounts retain their legacy manual link for now; employee Telegram
    # identities are managed separately by the employees module below.
    admin_account_cols = [row[1] for row in conn.execute("PRAGMA table_info(admin_accounts)").fetchall()]
    if "telegram_chat_id" not in admin_account_cols:
        conn.execute("ALTER TABLE admin_accounts ADD COLUMN telegram_chat_id TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS investors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            investor_name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER,
            employee_name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Legacy compatibility column. New personal links belong to
    # employee_telegram_accounts, while this value is mirrored so older
    # deployments and scripts continue to work during the transition.
    team_account_cols = [row[1] for row in conn.execute("PRAGMA table_info(team_accounts)").fetchall()]
    if "employee_id" not in team_account_cols:
        conn.execute("ALTER TABLE team_accounts ADD COLUMN employee_id INTEGER")
    if "telegram_chat_id" not in team_account_cols:
        conn.execute("ALTER TABLE team_accounts ADD COLUMN telegram_chat_id TEXT")
    conn.execute(
        "UPDATE team_accounts SET employee_id = "
        "(SELECT employees.id FROM employees WHERE employees.name = team_accounts.employee_name) "
        "WHERE employee_id IS NULL"
    )
    # Seed the known investor/team accounts if they don't exist yet.
    # Passwords only ever exist as pbkdf2 hashes here — nothing plaintext is
    # stored in the repo. pbkdf2 (not the werkzeug default of scrypt) is
    # used explicitly because some hosts' Python builds lack OpenSSL scrypt
    # support in hashlib, which makes scrypt-hashed logins fail at runtime.
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for admin_name, username, password_hash in ADMIN_ACCOUNTS:
        conn.execute(
            "INSERT OR IGNORE INTO admin_accounts (admin_name, username, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (admin_name, username, password_hash, now),
        )
    for investor_name, username, password_hash in INVESTOR_ACCOUNTS:
        conn.execute(
            "INSERT OR IGNORE INTO investors (investor_name, username, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (investor_name, username, password_hash, now),
        )
    for employee_name, username, password_hash in TEAM_ACCOUNTS:
        employee_row = conn.execute(
            "SELECT id FROM employees WHERE name = ? AND deleted_at IS NULL",
            (employee_name,),
        ).fetchone()
        if employee_row is None:
            continue
        employee_id = employee_row[0]
        # These constants only bootstrap missing accounts. Runtime password
        # resets made in «Сотрудники» are the source of truth and must survive
        # a restart, so an existing hash is deliberately never overwritten.
        existing = conn.execute(
            "SELECT id FROM team_accounts WHERE username = ?", (username,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO team_accounts "
                "(employee_id, employee_name, username, password_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (employee_id, employee_name, username, password_hash, now),
            )
        else:
            conn.execute(
                "UPDATE team_accounts SET employee_id = ?, employee_name = ? "
                "WHERE username = ?",
                (employee_id, employee_name, username),
            )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_contacts (
            chat_id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            last_text TEXT,
            last_message_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS employee_telegram_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL UNIQUE,
            chat_id TEXT NOT NULL UNIQUE,
            username TEXT,
            display_name TEXT,
            linked_at TEXT NOT NULL
        )
        """
    )
    # Preserve every personal Telegram link configured through the old
    # team_accounts column. The new table belongs to the employee rather
    # than to a login account, so employees without a cabinet can be linked.
    legacy_telegram_links = conn.execute(
        "SELECT employee_name, telegram_chat_id FROM team_accounts "
        "WHERE telegram_chat_id IS NOT NULL AND telegram_chat_id != ''"
    ).fetchall()
    for employee_name, chat_id in legacy_telegram_links:
        employee_row = conn.execute(
            "SELECT id FROM employees WHERE name = ?", (employee_name,)
        ).fetchone()
        if employee_row is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO telegram_contacts "
            "(chat_id, display_name, updated_at) VALUES (?, ?, ?)",
            (str(chat_id), employee_name, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO employee_telegram_accounts "
            "(employee_id, chat_id, display_name, linked_at) VALUES (?, ?, ?, ?)",
            (employee_row[0], str(chat_id), employee_name, now),
        )
    init_offline_schema(conn)
    init_software_requests_schema(conn)
    # Client relationships are classified only after all legacy tables have
    # received their newer client_id columns above.
    init_client_segments_schema(conn)
    init_ai_assistant_schema(conn)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------
# Недели (платежи еженедельные, неделя Пн–Вс)
# ---------------------------------------------------------------------

def week_bounds(d):
    monday = d - dt.timedelta(days=d.weekday())
    sunday = monday + dt.timedelta(days=6)
    return monday, sunday


def week_label(monday, sunday):
    if monday.month == sunday.month:
        return f"{monday.day}–{sunday.day} {MONTHS_GEN[monday.month - 1]} {monday.year}"
    if monday.year == sunday.year:
        return (f"{monday.day} {MONTHS_GEN[monday.month - 1]} – "
                f"{sunday.day} {MONTHS_GEN[sunday.month - 1]} {monday.year}")
    return (f"{monday.day} {MONTHS_GEN[monday.month - 1]} {monday.year} – "
            f"{sunday.day} {MONTHS_GEN[sunday.month - 1]} {sunday.year}")


def build_week_options(db):
    """Collect every week that has at least one entry, plus always include
    the current week so it can be selected even before anything is logged."""
    rows = db.execute("SELECT DISTINCT work_date FROM entries").fetchall()
    mondays = set()
    for row in rows:
        try:
            d = dt.date.fromisoformat(row["work_date"])
        except (ValueError, TypeError):
            continue
        monday, _ = week_bounds(d)
        mondays.add(monday)

    today = dt.date.today()
    current_monday, _ = week_bounds(today)
    mondays.add(current_monday)

    weeks = []
    for monday in sorted(mondays, reverse=True):
        sunday = monday + dt.timedelta(days=6)
        weeks.append({
            "key": monday.isoformat(),
            "label": week_label(monday, sunday),
            "is_current": monday == current_monday,
        })
    return weeks, current_monday


def compute_totals(entries):
    totals_by_employee = {}
    for e in entries:
        totals_by_employee.setdefault(e["employee"], 0.0)
        totals_by_employee[e["employee"]] += e["amount"]
    grand_total = sum(totals_by_employee.values())
    return totals_by_employee, grand_total


# ---------------------------------------------------------------------
# Месяцы (выплаты инвесторам считаются помесячно)
# ---------------------------------------------------------------------

def month_key(d):
    return f"{d.year:04d}-{d.month:02d}"


def month_label(d):
    return f"{MONTHS_GEN[d.month - 1].capitalize()} {d.year}"


def build_month_options(db):
    rows = db.execute("SELECT DISTINCT trip_date FROM trips").fetchall()
    keys = set()
    for row in rows:
        try:
            d = dt.date.fromisoformat(row["trip_date"])
        except (ValueError, TypeError):
            continue
        keys.add(month_key(d))

    today = dt.date.today()
    current_key = month_key(today)
    keys.add(current_key)

    months = []
    for key in sorted(keys, reverse=True):
        y, m = key.split("-")
        d = dt.date(int(y), int(m), 1)
        months.append({
            "key": key,
            "label": month_label(d),
            "is_current": key == current_key,
        })
    return months, current_key


def boat_lookup(name):
    for b in BOATS:
        if b["name"] == name:
            return b
    return None


def compute_trip_totals(trips):
    """Aggregate trips by boat and by investor for a summary view."""
    by_boat = {}
    for t in trips:
        b = by_boat.setdefault(t["boat"], {
            "revenue": 0.0, "commission": 0.0, "labor": 0.0, "fuel": 0.0,
            "mooring": 0.0, "extra": 0.0, "remainder": 0.0,
            "investor_payout": 0.0, "my_share": 0.0, "count": 0,
        })
        b["revenue"] += t["revenue"]
        b["commission"] += t["commission_amount"]
        b["labor"] += t["labor_cost"]
        b["fuel"] += t["fuel_cost"]
        b["mooring"] += t["mooring_cost"]
        b["extra"] += t["extra_total"]
        b["remainder"] += t["remainder"]
        b["investor_payout"] += t["investor_payout"]
        b["my_share"] += t["my_share"]
        b["count"] += 1

    by_investor = {}
    for boat_name, totals in by_boat.items():
        boat = boat_lookup(boat_name)
        investor = boat["investor"] if boat else "Неизвестный катер"
        inv = by_investor.setdefault(investor, {"payout": 0.0, "boats": {}})
        inv["payout"] += totals["investor_payout"]
        inv["boats"][boat_name] = totals["investor_payout"]

    grand_my_share = sum(b["my_share"] for b in by_boat.values())
    grand_revenue = sum(b["revenue"] for b in by_boat.values())
    return by_boat, by_investor, grand_my_share, grand_revenue


def _payroll_context(db, selected_week, selected_employee):
    """Everything the Зарплаты page needs to render: the entries table,
    per-employee/grand totals, which employees are already marked paid for
    the selected week, and the week/employee filter options. Shared by the
    main page and by both "add" routes' validation-error re-renders, so
    they can't drift out of sync (the plain add_entry() error path once
    rendered the totals cards without paid_employees at all)."""
    weeks, current_monday = build_week_options(db)

    # Left-joined to the trip (if any) this entry came from, purely to show
    # its start time alongside the date — entries added by hand on this
    # page have no trip and so no time, which is fine (trip_time comes
    # back NULL and the template just shows the date on its own).
    query = (
        "SELECT entries.*, trips.trip_time AS trip_time FROM entries "
        "LEFT JOIN trip_labor ON trip_labor.entry_id = entries.id "
        "LEFT JOIN trips ON trips.id = trip_labor.trip_id "
        "WHERE 1=1"
    )
    params = []

    if selected_week != "all":
        try:
            monday = dt.date.fromisoformat(selected_week)
        except ValueError:
            monday = current_monday
            selected_week = monday.isoformat()
        sunday = monday + dt.timedelta(days=6)
        query += " AND entries.work_date BETWEEN ? AND ?"
        params += [monday.isoformat(), sunday.isoformat()]

    if selected_employee != "all":
        query += " AND entries.employee = ?"
        params.append(selected_employee)

    query += " ORDER BY entries.work_date DESC, entries.id DESC"
    entries = db.execute(query, params).fetchall()

    totals_by_employee, grand_total = compute_totals(entries)

    # "Оплачено" only means anything for one concrete week at a time — an
    # "Все периоды" total spans however many weeks, so there's no single
    # period to mark paid.
    paid_employees = set()
    tbank_payouts = {}
    if selected_week != "all":
        paid_employees = {
            row["employee"] for row in db.execute(
                "SELECT employee FROM payments WHERE period_key = ?", (selected_week,)
            ).fetchall()
        }
        # Latest attempt per employee for this week — iterating id DESC and
        # using setdefault means the first (most recent) row wins.
        for row in db.execute(
            "SELECT * FROM tbank_payout_registries WHERE period_key = ? ORDER BY id DESC",
            (selected_week,),
        ).fetchall():
            tbank_payouts.setdefault(row["employee"], dict(row))

    # Keep active employees first, then append historical names from old
    # payroll rows so deleting access never hides previously earned amounts.
    known = _active_employee_names(db)
    for row in db.execute("SELECT DISTINCT employee FROM entries").fetchall():
        if row["employee"] not in known:
            known.append(row["employee"])

    projects = db.execute(
        "SELECT projects.*, tuning_orders.client_name AS client_name, "
        "tuning_orders.boat_model AS boat_model, "
        "tuning_orders.boat_registration_number AS boat_registration_number, "
        "tuning_orders.equipment_type AS equipment_type, "
        "tuning_orders.motor_model AS motor_model, "
        "tuning_orders.motor_serial_number AS motor_serial_number "
        "FROM projects LEFT JOIN tuning_orders ON tuning_orders.id = projects.tuning_order_id "
        "ORDER BY projects.created_at DESC, projects.id DESC"
    ).fetchall()

    # For the "Вид работы" dropdown: when a project tied to a tuning order is
    # selected, offer that order's own nomenclature (its tuning_order_items)
    # as work-type choices too — keyed by project id for the JS on the form.
    project_items = {}
    for row in db.execute(
        "SELECT projects.id AS project_id, tuning_order_items.work_name AS work_name "
        "FROM projects JOIN tuning_order_items ON tuning_order_items.order_id = projects.tuning_order_id "
        "WHERE projects.tuning_order_id IS NOT NULL "
        "ORDER BY projects.id, tuning_order_items.id"
    ).fetchall():
        project_items.setdefault(row["project_id"], []).append(row["work_name"])

    return dict(
        entries=entries,
        totals_by_employee=totals_by_employee,
        grand_total=grand_total,
        weeks=weeks,
        selected_week=selected_week,
        employees_filter=known,
        selected_employee=selected_employee,
        paid_employees=paid_employees,
        tbank_payouts=tbank_payouts,
        tbank_configured=tbank_payment_configured(),
        projects=projects,
        project_items=project_items,
    )


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/sw.js")
def service_worker():
    # Served from the domain root (not /static/sw.js) on purpose: a service
    # worker's default scope is the directory it's served from, and this
    # one needs to cover the whole site (every /admin, /team/, /client/...
    # page), not just /static/.
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")


def admin_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


def _active_team_account(db):
    team_id = session.get("team_id")
    if not team_id:
        return None
    return db.execute(
        "SELECT team_accounts.id, team_accounts.employee_id, employees.name "
        "FROM team_accounts JOIN employees "
        "ON employees.id = team_accounts.employee_id "
        "WHERE team_accounts.id = ? AND employees.deleted_at IS NULL",
        (team_id,),
    ).fetchone()


def _is_customer_manager(db=None):
    if session.get("admin_id") or not session.get("team_id"):
        return False
    db = db or get_db()
    account = _active_team_account(db)
    if account is None:
        return False
    return db.execute(
        "SELECT 1 FROM employee_positions WHERE employee_id = ? "
        "AND position = ?",
        (account["employee_id"], CUSTOMER_MANAGER_POSITION),
    ).fetchone() is not None


def _current_ai_user():
    """Resolve the signed-in staff member and their data-access scope."""
    db = get_db()
    admin_id = session.get("admin_id")
    if admin_id:
        row = db.execute(
            "SELECT id, admin_name FROM admin_accounts WHERE id = ?",
            (admin_id,),
        ).fetchone()
        if row is not None:
            return {
                "owner_type": "admin",
                "owner_id": row["id"],
                "name": row["admin_name"],
                "positions": ["Администратор"],
                "manager_view": False,
            }

    account = _active_team_account(db)
    if account is None:
        return None
    positions = [
        row["position"]
        for row in db.execute(
            "SELECT position FROM employee_positions "
            "WHERE employee_id = ? ORDER BY position COLLATE NOCASE",
            (account["employee_id"],),
        ).fetchall()
    ]
    return {
        "owner_type": "employee",
        "owner_id": account["employee_id"],
        "name": account["name"],
        "positions": positions,
        "manager_view": CUSTOMER_MANAGER_POSITION in positions,
    }


def excursion_manager_or_admin_required(view):
    """Allow administrators and customer managers into excursion tools."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("admin_id"):
            return view(*args, **kwargs)
        if _is_customer_manager():
            return view(*args, **kwargs)
        if session.get("team_id"):
            if _active_team_account(get_db()) is None:
                session.clear()
                return redirect(url_for("team_login"))
            return redirect(url_for("team_dashboard"))
        return redirect(url_for("admin_login"))
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "GET":
        if session.get("admin_id"):
            return redirect(url_for("index"))
        return render_template("admin_login.html", error=None)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    db = get_db()
    row = db.execute(
        "SELECT * FROM admin_accounts WHERE username = ?", (username,)
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return render_template(
            "admin_login.html", error="Неверный логин или пароль.",
        ), 401

    session.clear()
    session["admin_id"] = row["id"]
    session["admin_name"] = row["admin_name"]
    return redirect(url_for("index"))


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/push/vapid-public-key")
@admin_login_required
def push_vapid_public_key():
    return VAPID_PUBLIC_KEY or "", 200, {"Content-Type": "text/plain"}


@app.route("/push/subscribe", methods=["POST"])
@admin_login_required
def push_subscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint", "").strip()
    keys = data.get("keys") or {}
    p256dh = keys.get("p256dh", "").strip()
    auth = keys.get("auth", "").strip()
    if not (endpoint and p256dh and auth):
        return jsonify({"ok": False, "error": "incomplete subscription"}), 400
    db = get_db()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    # Plain select-then-insert/update, not "ON CONFLICT ... DO UPDATE" — that
    # SQLite syntax needs 3.24+, which this host's Python isn't necessarily
    # built against (see the team_accounts seeding fix elsewhere in init_db).
    existing = db.execute(
        "SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
    ).fetchone()
    if existing is None:
        db.execute(
            "INSERT INTO push_subscriptions (role, endpoint, p256dh, auth, created_at) "
            "VALUES ('admin', ?, ?, ?, ?)",
            (endpoint, p256dh, auth, now),
        )
    else:
        db.execute(
            "UPDATE push_subscriptions SET p256dh = ?, auth = ? WHERE endpoint = ?",
            (p256dh, auth, endpoint),
        )
    db.commit()
    return jsonify({"ok": True})


@app.route("/push/unsubscribe", methods=["POST"])
@admin_login_required
def push_unsubscribe():
    data = request.get_json(silent=True) or {}
    endpoint = data.get("endpoint", "").strip()
    if endpoint:
        db = get_db()
        db.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        db.commit()
    return jsonify({"ok": True})


@app.route("/admin")
@admin_login_required
def index():
    db = get_db()
    weeks, current_monday = build_week_options(db)
    selected_week = request.args.get("week", current_monday.isoformat())
    selected_employee = request.args.get("employee", "all")

    ctx = _payroll_context(db, selected_week, selected_employee)
    return render_template(
        "index.html",
        **ctx,
        employees_form=_active_employee_names(db),
        work_types=WORK_TYPES,
        custom_value=CUSTOM_VALUE,
        today=dt.date.today().isoformat(),
        active_page="payroll",
    )


@app.route("/pay", methods=["POST"])
@admin_login_required
def mark_paid():
    employee = request.form.get("employee", "").strip()
    period_key = request.form.get("week", "").strip()
    employee_filter = request.form.get("employee_filter", "all")
    if employee and period_key:
        db = get_db()
        db.execute(
            "INSERT OR IGNORE INTO payments (employee, period_key, paid_at) VALUES (?, ?, ?)",
            (employee, period_key, dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        db.commit()
    return redirect(url_for("index", week=period_key, employee=employee_filter))


@app.route("/unpay", methods=["POST"])
@admin_login_required
def unmark_paid():
    employee = request.form.get("employee", "").strip()
    period_key = request.form.get("week", "").strip()
    employee_filter = request.form.get("employee_filter", "all")
    if employee and period_key:
        db = get_db()
        db.execute(
            "DELETE FROM payments WHERE employee = ? AND period_key = ?",
            (employee, period_key),
        )
        db.commit()
    return redirect(url_for("index", week=period_key, employee=employee_filter))


@app.route("/pay/tbank", methods=["POST"])
@admin_login_required
def tbank_create_payout():
    employee = request.form.get("employee", "").strip()
    period_key = request.form.get("week", "").strip()
    employee_filter = request.form.get("employee_filter", "all")
    if employee and period_key and tbank_payment_configured():
        db = get_db()
        # Recompute the amount server-side from entries rather than trust a
        # client-submitted figure — it must match exactly what the totals
        # card shows, and this is real money leaving the account.
        ctx = _payroll_context(db, period_key, "all")
        amount = ctx["totals_by_employee"].get(employee)
        if amount:
            _tbank_send_payout(db, employee, period_key, amount)
    return redirect(url_for("index", week=period_key, employee=employee_filter))


@app.route("/add", methods=["POST"])
@admin_login_required
def add_entry():
    employee = request.form.get("employee", "").strip()
    if employee == CUSTOM_VALUE:
        employee = request.form.get("employee_custom", "").strip()

    work_type = request.form.get("work_type", "").strip()
    if work_type == CUSTOM_VALUE:
        work_type = request.form.get("work_type_custom", "").strip()

    rate_raw = request.form.get("rate", "").strip().replace(",", ".")
    quantity_raw = request.form.get("quantity", "").strip().replace(",", ".")
    work_date_raw = request.form.get("work_date", "").strip()
    project_id_raw = request.form.get("project_id", "").strip()
    project_id = int(project_id_raw) if project_id_raw.isdigit() else None

    errors = []
    if not employee:
        errors.append("Укажите сотрудника.")
    if not work_type:
        errors.append("Укажите вид работы.")

    try:
        rate = float(rate_raw)
        if rate < 0:
            errors.append("Ставка не может быть отрицательной.")
    except ValueError:
        rate = None
        errors.append("Ставка должна быть числом.")

    try:
        quantity = float(quantity_raw)
        if quantity < 0:
            errors.append("Часы/количество не может быть отрицательным.")
    except ValueError:
        quantity = None
        errors.append("Часы/количество должно быть числом.")

    try:
        work_date = dt.date.fromisoformat(work_date_raw).isoformat()
    except ValueError:
        work_date = dt.date.today().isoformat()

    if errors:
        db = get_db()
        ctx = _payroll_context(db, "all", "all")
        return render_template(
            "index.html",
            **ctx,
            employees_form=_active_employee_names(db),
            work_types=WORK_TYPES,
            custom_value=CUSTOM_VALUE,
            today=dt.date.today().isoformat(),
            errors=errors,
            form_values=request.form,
            active_page="payroll",
        ), 400

    amount = rate * quantity
    db = get_db()
    db.execute(
        "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (employee, work_type, rate, quantity, amount, work_date,
         dt.datetime.now().strftime("%Y-%m-%d %H:%M"), project_id),
    )
    db.commit()
    return redirect(url_for("index"))


@app.route("/add_manager_fee", methods=["POST"])
@admin_login_required
def add_manager_fee():
    employee = request.form.get("manager_employee", "").strip()
    if employee == CUSTOM_VALUE:
        employee = request.form.get("manager_employee_custom", "").strip()

    shifts_raw = request.form.get("shifts", "").strip().replace(",", ".")
    week = request.form.get("week", "").strip()
    employee_filter = request.form.get("employee_filter", "all")

    errors = []
    if not employee:
        errors.append("Укажите сотрудника.")

    monday = None
    try:
        monday = dt.date.fromisoformat(week)
    except ValueError:
        errors.append("Расчёт гонорара доступен только для одной конкретной недели — выберите её в фильтре выше.")

    shifts = None
    try:
        shifts = float(shifts_raw)
        if shifts < 0:
            errors.append("Количество смен не может быть отрицательным.")
    except ValueError:
        errors.append("Количество смен должно быть числом.")

    if errors:
        db = get_db()
        ctx = _payroll_context(db, week or "all", employee_filter)
        return render_template(
            "index.html",
            **ctx,
            employees_form=_active_employee_names(db),
            work_types=WORK_TYPES,
            custom_value=CUSTOM_VALUE,
            today=dt.date.today().isoformat(),
            manager_errors=errors,
            manager_form_values=request.form,
            active_page="payroll",
        ), 400

    sunday = monday + dt.timedelta(days=6)
    db = get_db()
    revenue_week = db.execute(
        "SELECT COALESCE(SUM(revenue), 0) AS total FROM trips WHERE trip_date BETWEEN ? AND ?",
        (monday.isoformat(), sunday.isoformat()),
    ).fetchone()["total"]

    # Оклад считается от количества дней в текущем календарном месяце —
    # расчёт делается "сейчас", по факту закрытия недели.
    days_in_month = calendar.monthrange(dt.date.today().year, dt.date.today().month)[1]
    percent_part = revenue_week * MANAGER_REVENUE_SHARE / 7 * shifts
    salary_part = MANAGER_BASE_SALARY / days_in_month * shifts
    amount = percent_part + salary_part
    rate = amount / shifts if shifts else 0.0

    db.execute(
        "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (employee, MANAGER_FEE_WORK_TYPE, rate, shifts, amount, monday.isoformat(),
         dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    db.commit()
    return redirect(url_for("index", week=monday.isoformat(), employee=employee_filter))


@app.route("/delete/<int:entry_id>", methods=["POST"])
@admin_login_required
def delete_entry(entry_id):
    db = get_db()
    db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    db.commit()
    return redirect(url_for("index"))


def _suggest_contract_number(db, today):
    """Next contract number in the template's own DDMMYY-N convention
    (see "Договор оказания услуг 200825-1" in the template) — N is how
    many contracts already exist for today, plus one. Just a prefill the
    admin can overwrite; nothing enforces it stays unique or sequential."""
    prefix = today.strftime("%d%m%y")
    count_today = db.execute(
        "SELECT COUNT(*) AS n FROM trip_contracts WHERE contract_number LIKE ?",
        (f"{prefix}-%",),
    ).fetchone()["n"]
    return f"{prefix}-{count_today + 1}"


def _trips_list_context(db, selected_month=None, selected_boat="all"):
    months, current_key = build_month_options(db)
    if not selected_month:
        selected_month = current_key

    query = "SELECT * FROM trips WHERE 1=1"
    params = []
    if selected_month != "all":
        query += " AND substr(trip_date, 1, 7) = ?"
        params.append(selected_month)
    if selected_boat != "all":
        query += " AND boat = ?"
        params.append(selected_boat)
    query += " ORDER BY trip_date DESC, id DESC"
    trip_rows = db.execute(query, params).fetchall()

    trips_list = []
    for t in trip_rows:
        exps = db.execute(
            "SELECT * FROM trip_expenses WHERE trip_id = ?", (t["id"],)
        ).fetchall()
        trips_list.append({**dict(t), "expenses": exps})

    by_boat, by_investor, grand_my_share, grand_revenue = compute_trip_totals(trip_rows)

    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    import_candidates = db.execute(
        "SELECT * FROM import_candidates ORDER BY created_at DESC, id DESC"
    ).fetchall()

    contracts = db.execute(
        "SELECT * FROM trip_contracts ORDER BY id DESC"
    ).fetchall()

    return dict(
        trips=trips_list,
        months=months,
        selected_month=selected_month,
        boats=BOATS,
        selected_boat=selected_boat,
        by_boat=by_boat,
        by_investor=by_investor,
        grand_my_share=grand_my_share,
        grand_revenue=grand_revenue,
        import_candidates=import_candidates,
        import_configured=yclients_configured(),
        import_default_start=week_ago.isoformat(),
        import_default_end=today.isoformat(),
        contracts=contracts,
        contract_number_suggestion=_suggest_contract_number(db, today),
    )


def _trips_common_kwargs(db):
    return dict(
        employees_form=_active_employee_names(db),
        work_types=WORK_TYPES,
        sale_channels=SALE_CHANNELS,
        custom_value=CUSTOM_VALUE,
        today=dt.date.today().isoformat(),
        now_time=dt.datetime.now().strftime("%H:%M"),
        active_page="trips",
    )


def _process_trip_form(db, form, exclude_trip_id=None):
    """Validate a trip form submission and compute all derived amounts.
    Returns (errors, data). data is None if there are errors."""
    errors = []

    boat = form.get("boat", "").strip()
    if not boat or not boat_lookup(boat):
        errors.append("Выберите катер.")

    trip_date_raw = form.get("trip_date", "").strip()
    try:
        trip_date = dt.date.fromisoformat(trip_date_raw).isoformat()
    except ValueError:
        trip_date = dt.date.today().isoformat()

    trip_time_raw = form.get("trip_time", "").strip()
    try:
        trip_time = dt.datetime.strptime(trip_time_raw, "%H:%M").strftime("%H:%M")
    except ValueError:
        trip_time = "00:00"

    # --- Labor rows: one or more employees paid for this trip ---
    employees_raw = form.getlist("employee[]")
    employees_custom_raw = form.getlist("employee_custom[]")
    work_types_raw = form.getlist("work_type[]")
    work_types_custom_raw = form.getlist("work_type_custom[]")
    quantities_raw = form.getlist("quantity[]")
    rates_raw = form.getlist("rate[]")

    def _get(lst, i):
        return lst[i] if i < len(lst) else ""

    labor_items = []
    labor_cost = 0.0
    for i in range(len(employees_raw)):
        emp = employees_raw[i].strip()
        if emp == CUSTOM_VALUE:
            emp = _get(employees_custom_raw, i).strip()
        wt = _get(work_types_raw, i).strip()
        if wt == CUSTOM_VALUE:
            wt = _get(work_types_custom_raw, i).strip()
        q_raw = _get(quantities_raw, i).strip().replace(",", ".")
        r_raw = _get(rates_raw, i).strip().replace(",", ".")

        if not emp and not wt and not q_raw and not r_raw:
            continue  # fully empty row — ignore silently

        row_num = i + 1
        if not emp:
            errors.append(f"Сотрудник №{row_num}: не указано имя.")
        if not wt:
            errors.append(f"Сотрудник №{row_num}: не указан вид рейса.")

        q = r = None
        try:
            q = float(q_raw)
            if q < 0:
                errors.append(f"Сотрудник №{row_num}: часы не могут быть отрицательными.")
        except ValueError:
            errors.append(f"Сотрудник №{row_num}: часы должны быть числом.")
        try:
            r = float(r_raw)
            if r < 0:
                errors.append(f"Сотрудник №{row_num}: ставка не может быть отрицательной.")
        except ValueError:
            errors.append(f"Сотрудник №{row_num}: ставка должна быть числом.")

        if emp and wt and q is not None and r is not None:
            amount = q * r
            labor_items.append({"employee": emp, "work_type": wt, "quantity": q, "rate": r, "amount": amount})
            labor_cost += amount

    if not labor_items and not any("Сотрудник" in e for e in errors):
        errors.append("Добавьте хотя бы одного сотрудника (капитан/гид) на рейс.")

    def parse_num(name, label):
        raw = form.get(name, "").strip().replace(",", ".")
        try:
            v = float(raw)
            if v < 0:
                errors.append(f"«{label}» не может быть отрицательным.")
            return v
        except ValueError:
            errors.append(f"«{label}» должно быть числом.")
            return None

    revenue = parse_num("revenue", "Доход рейса")
    commission_pct = parse_num("commission_pct", "Комиссия (%)")
    fuel_cost = parse_num("fuel_cost", "Топливо")
    mooring_cost = parse_num("mooring_cost", "Стоянка")

    sale_channel = form.get("sale_channel", "direct").strip()
    if sale_channel not in [c["value"] for c in SALE_CHANNELS]:
        sale_channel = "direct"

    descs = form.getlist("expense_desc[]")
    amounts = form.getlist("expense_amount[]")
    expenses = []
    extra_total = 0.0
    for d_, a_ in zip(descs, amounts):
        d_ = d_.strip()
        a_raw = a_.strip().replace(",", ".")
        if not d_ and not a_raw:
            continue
        if not d_:
            errors.append("У дополнительного расхода не указано описание.")
            continue
        try:
            av = float(a_raw)
        except ValueError:
            errors.append(f"Сумма расхода «{d_}» должна быть числом.")
            continue
        expenses.append((d_, av))
        extra_total += av

    if errors:
        return errors, None

    # "Стоянка" — суточный расход катера, а не расход конкретного рейса: если
    # в этот день у этого катера уже есть рейс с оплаченной стоянкой, не
    # дублируем её здесь.
    if mooring_cost and boat and trip_date:
        query = "SELECT 1 FROM trips WHERE boat = ? AND trip_date = ? AND mooring_cost > 0"
        params = [boat, trip_date]
        if exclude_trip_id is not None:
            query += " AND id != ?"
            params.append(exclude_trip_id)
        if db.execute(query, params).fetchone() is not None:
            mooring_cost = 0.0

    # Trip-level "вид рейса" label for lists/summaries: the distinct work
    # types among the labor rows, joined (usually just one).
    distinct_work_types = []
    for item in labor_items:
        if item["work_type"] not in distinct_work_types:
            distinct_work_types.append(item["work_type"])
    work_type_label = " + ".join(distinct_work_types)

    commission_amount = revenue * commission_pct / 100
    direct_costs = labor_cost + fuel_cost + mooring_cost + extra_total
    remainder = revenue - commission_amount - direct_costs
    investor_payout = remainder / 2
    my_share = commission_amount + remainder / 2

    data = dict(
        boat=boat, trip_date=trip_date, trip_time=trip_time, work_type=work_type_label,
        labor_items=labor_items, labor_cost=labor_cost,
        revenue=revenue, sale_channel=sale_channel, commission_pct=commission_pct,
        commission_amount=commission_amount, fuel_cost=fuel_cost, mooring_cost=mooring_cost,
        extra_total=extra_total, expenses=expenses, remainder=remainder,
        investor_payout=investor_payout, my_share=my_share,
    )
    return errors, data


@app.route("/trips")
@admin_login_required
def trips_index():
    db = get_db()
    selected_month = request.args.get("month")
    selected_boat = request.args.get("boat", "all")
    ctx = _trips_list_context(db, selected_month, selected_boat)
    return render_template(
        "trips.html", **ctx, **_trips_common_kwargs(db), edit_trip=None,
        trip_expense_error=session.pop("trip_expense_error", None),
        trip_contract_error=session.pop("trip_contract_error", None),
    )


@app.route("/trips/add", methods=["POST"])
@admin_login_required
def add_trip():
    db = get_db()
    errors, data = _process_trip_form(db, request.form)
    if errors:
        ctx = _trips_list_context(db)
        return render_template(
            "trips.html", **ctx, **_trips_common_kwargs(db),
            edit_trip=None, errors=errors, form_values=request.form,
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    entry_ids = []
    for item in data["labor_items"]:
        cur = db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item["employee"], item["work_type"], item["rate"], item["quantity"],
             item["amount"], data["trip_date"], now),
        )
        entry_ids.append(cur.lastrowid)

    cur2 = db.execute(
        "INSERT INTO trips (boat, trip_date, trip_time, work_type, entry_id, revenue, sale_channel, "
        "commission_pct, commission_amount, labor_cost, fuel_cost, mooring_cost, extra_total, "
        "remainder, investor_payout, my_share, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (data["boat"], data["trip_date"], data["trip_time"], data["work_type"],
         entry_ids[0] if entry_ids else None,
         data["revenue"], data["sale_channel"], data["commission_pct"], data["commission_amount"],
         data["labor_cost"], data["fuel_cost"], data["mooring_cost"], data["extra_total"],
         data["remainder"], data["investor_payout"], data["my_share"], now),
    )
    trip_id = cur2.lastrowid
    for eid in entry_ids:
        db.execute(
            "INSERT INTO trip_labor (trip_id, entry_id) VALUES (?, ?)", (trip_id, eid)
        )
    for desc, amt in data["expenses"]:
        db.execute(
            "INSERT INTO trip_expenses (trip_id, description, amount) VALUES (?, ?, ?)",
            (trip_id, desc, amt),
        )
    db.commit()
    return redirect(url_for("trips_index"))


@app.route("/trips/edit/<int:trip_id>", methods=["GET", "POST"])
@admin_login_required
def edit_trip(trip_id):
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        return redirect(url_for("trips_index"))

    if request.method == "GET":
        labor_links = db.execute(
            "SELECT entry_id FROM trip_labor WHERE trip_id = ?", (trip_id,)
        ).fetchall()
        entry_ids = [l["entry_id"] for l in labor_links]
        if not entry_ids and trip["entry_id"]:
            entry_ids = [trip["entry_id"]]  # legacy trips created before multi-employee support

        labor_prefill = []
        for eid in entry_ids:
            e = db.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
            if e:
                labor_prefill.append({
                    "employee": e["employee"], "work_type": e["work_type"],
                    "quantity": e["quantity"], "rate": e["rate"],
                })
        if not labor_prefill:
            labor_prefill = [{"employee": "", "work_type": "", "quantity": "", "rate": ""}]

        exps = db.execute(
            "SELECT * FROM trip_expenses WHERE trip_id = ?", (trip_id,)
        ).fetchall()
        form_values = {
            "boat": trip["boat"],
            "trip_date": trip["trip_date"],
            "trip_time": trip["trip_time"] or "00:00",
            "revenue": trip["revenue"],
            "sale_channel": trip["sale_channel"],
            "commission_pct": trip["commission_pct"],
            "fuel_cost": trip["fuel_cost"],
            "mooring_cost": trip["mooring_cost"],
        }
        ctx = _trips_list_context(db)
        return render_template(
            "trips.html", **ctx, **_trips_common_kwargs(db),
            edit_trip=trip, form_values=form_values,
            labor_prefill=labor_prefill,
            expenses_prefill=[(e["description"], e["amount"]) for e in exps],
        )

    errors, data = _process_trip_form(db, request.form, exclude_trip_id=trip_id)
    if errors:
        ctx = _trips_list_context(db)
        return render_template(
            "trips.html", **ctx, **_trips_common_kwargs(db),
            edit_trip=trip, errors=errors, form_values=request.form,
        ), 400

    # Remove all previously linked labor entries (legacy single entry_id too),
    # then recreate fresh ones from the submitted rows — simplest way to keep
    # everything in sync without trying to match rows by position.
    labor_links = db.execute(
        "SELECT entry_id FROM trip_labor WHERE trip_id = ?", (trip_id,)
    ).fetchall()
    old_entry_ids = {l["entry_id"] for l in labor_links}
    if trip["entry_id"]:
        old_entry_ids.add(trip["entry_id"])
    for eid in old_entry_ids:
        db.execute("DELETE FROM entries WHERE id = ?", (eid,))
    db.execute("DELETE FROM trip_labor WHERE trip_id = ?", (trip_id,))

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry_ids = []
    for item in data["labor_items"]:
        cur = db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item["employee"], item["work_type"], item["rate"], item["quantity"],
             item["amount"], data["trip_date"], now),
        )
        entry_ids.append(cur.lastrowid)
    for eid in entry_ids:
        db.execute("INSERT INTO trip_labor (trip_id, entry_id) VALUES (?, ?)", (trip_id, eid))

    db.execute(
        "UPDATE trips SET boat=?, trip_date=?, trip_time=?, work_type=?, entry_id=?, revenue=?, "
        "sale_channel=?, commission_pct=?, commission_amount=?, labor_cost=?, fuel_cost=?, "
        "mooring_cost=?, extra_total=?, remainder=?, investor_payout=?, my_share=? WHERE id=?",
        (data["boat"], data["trip_date"], data["trip_time"], data["work_type"],
         entry_ids[0] if entry_ids else None, data["revenue"],
         data["sale_channel"], data["commission_pct"], data["commission_amount"],
         data["labor_cost"], data["fuel_cost"], data["mooring_cost"], data["extra_total"],
         data["remainder"], data["investor_payout"], data["my_share"], trip_id),
    )
    db.execute("DELETE FROM trip_expenses WHERE trip_id = ?", (trip_id,))
    for desc, amt in data["expenses"]:
        db.execute(
            "INSERT INTO trip_expenses (trip_id, description, amount) VALUES (?, ?, ?)",
            (trip_id, desc, amt),
        )
    db.commit()
    return redirect(url_for("trips_index"))


def _delete_trip_data(db, trip_id, *, release_yclients_refs=False):
    """Delete a trip and its owned rows.

    A manual deletion keeps YCLIENTS references as tombstones so the next
    hourly job does not recreate the trip against the administrator's will.
    A YCLIENTS cancellation releases those references: if the cancellation
    is later reversed, the booking may be imported again normally.
    """
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        return False
    labor_links = db.execute(
        "SELECT entry_id FROM trip_labor WHERE trip_id = ?", (trip_id,)
    ).fetchall()
    entry_ids = {link["entry_id"] for link in labor_links}
    if trip["entry_id"]:
        entry_ids.add(trip["entry_id"])
    for entry_id in entry_ids:
        db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
    db.execute("DELETE FROM trip_labor WHERE trip_id = ?", (trip_id,))
    db.execute("DELETE FROM trip_expenses WHERE trip_id = ?", (trip_id,))
    if release_yclients_refs:
        db.execute("DELETE FROM yclients_imports WHERE trip_id = ?", (trip_id,))
    db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
    return True


@app.route("/trips/delete/<int:trip_id>", methods=["POST"])
@admin_login_required
def delete_trip(trip_id):
    db = get_db()
    if _delete_trip_data(db, trip_id):
        db.commit()
    return redirect(url_for("trips_index"))


@app.route("/trips/expense/add", methods=["POST"])
@admin_login_required
def add_trip_expense():
    db = get_db()
    boat = request.form.get("boat", "").strip()
    expense_date = request.form.get("expense_date", "").strip()
    description = request.form.get("description", "").strip()
    amount_raw = request.form.get("amount", "").strip().replace(",", ".")
    employee = request.form.get("employee", "").strip()

    errors = []
    if boat not in [b["name"] for b in BOATS]:
        errors.append("Выберите катер.")
    try:
        expense_date and dt.date.fromisoformat(expense_date)
    except ValueError:
        errors.append("Некорректная дата.")
    if not expense_date:
        errors.append("Укажите дату расхода.")
    if not description:
        errors.append("Укажите описание расхода.")
    try:
        amount = float(amount_raw)
        if amount <= 0:
            errors.append("Сумма расхода должна быть больше нуля.")
    except ValueError:
        amount = None
        errors.append("Сумма расхода должна быть числом.")
    if employee and employee not in _active_employee_names(db):
        errors.append("Неизвестный сотрудник.")

    if errors:
        session["trip_expense_error"] = " ".join(errors)
        return redirect(url_for("trips_index"))

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    remainder = -amount

    entry_id = None
    if employee:
        cur = db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (employee, description, amount, 1, amount, expense_date, now),
        )
        entry_id = cur.lastrowid

    cur2 = db.execute(
        "INSERT INTO trips (boat, trip_date, trip_time, work_type, entry_id, revenue, sale_channel, "
        "commission_pct, commission_amount, labor_cost, fuel_cost, mooring_cost, extra_total, "
        "remainder, investor_payout, my_share, created_at, is_expense) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (boat, expense_date, "00:00", description, entry_id, 0.0, "direct",
         0.0, 0.0, 0.0, 0.0, 0.0, amount,
         remainder, remainder / 2, remainder / 2, now, 1),
    )
    if entry_id is not None:
        db.execute(
            "INSERT INTO trip_labor (trip_id, entry_id) VALUES (?, ?)",
            (cur2.lastrowid, entry_id),
        )
    db.commit()
    return redirect(url_for("trips_index"))


@app.route("/trips/contracts/generate", methods=["POST"])
@admin_login_required
def generate_trip_contract():
    db = get_db()
    form = request.form

    contract_number = form.get("contract_number", "").strip()
    contract_date_raw = form.get("contract_date", "").strip()
    client_name = form.get("client_name", "").strip()
    client_representative = form.get("client_representative", "").strip()
    client_representative_basis = form.get("client_representative_basis", "").strip()
    client_requisites = form.get("client_requisites", "").strip()
    service_description = form.get("service_description", "").strip()
    service_date_raw = form.get("service_date", "").strip()
    service_time = form.get("service_time", "").strip()
    total_amount_raw = form.get("total_amount", "").strip().replace(",", ".")
    prepayment_terms = form.get("prepayment_terms", "").strip()
    prepayment_amount_raw = form.get("prepayment_amount", "").strip().replace(",", ".")

    errors = []
    if not contract_number:
        errors.append("Укажите номер договора.")
    if not client_name:
        errors.append("Укажите заказчика.")
    if not service_description:
        errors.append("Укажите описание услуги.")

    try:
        contract_date = dt.date.fromisoformat(contract_date_raw).isoformat() if contract_date_raw else dt.date.today().isoformat()
    except ValueError:
        errors.append("Некорректная дата договора.")
        contract_date = dt.date.today().isoformat()

    try:
        service_date = dt.date.fromisoformat(service_date_raw).isoformat() if service_date_raw else None
    except ValueError:
        errors.append("Некорректная дата оказания услуги.")
        service_date = None

    total_amount = None
    try:
        total_amount = float(total_amount_raw)
        if total_amount <= 0:
            errors.append("Стоимость услуг должна быть больше нуля.")
    except ValueError:
        errors.append("Стоимость услуг должна быть числом.")

    prepayment_amount = None
    if prepayment_amount_raw:
        try:
            prepayment_amount = float(prepayment_amount_raw)
        except ValueError:
            errors.append("Размер предоплаты должен быть числом.")

    if errors:
        session["trip_contract_error"] = " ".join(errors)
        return redirect(url_for("trips_index"))

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = db.execute(
        "INSERT INTO trip_contracts (contract_number, contract_date, client_name, "
        "client_representative, client_representative_basis, client_requisites, "
        "service_description, service_date, service_time, total_amount, "
        "prepayment_terms, prepayment_amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (contract_number, contract_date, client_name, client_representative or None,
         client_representative_basis or None, client_requisites or None,
         service_description, service_date, service_time or None, total_amount,
         prepayment_terms or None, prepayment_amount, now),
    )
    db.commit()
    return redirect(url_for("download_trip_contract", contract_id=cur.lastrowid))


@app.route("/trips/contracts/<int:contract_id>/download")
@admin_login_required
def download_trip_contract(contract_id):
    db = get_db()
    row = db.execute("SELECT * FROM trip_contracts WHERE id = ?", (contract_id,)).fetchone()
    if row is None:
        return redirect(url_for("trips_index"))

    data = {
        "contract_number": row["contract_number"],
        "contract_date": format_ru_date(row["contract_date"]).replace("/", "."),
        "client_name": row["client_name"],
        "client_representative": row["client_representative"] or "",
        "client_representative_basis": row["client_representative_basis"] or "",
        "client_requisites": row["client_requisites"] or "",
        "service_description": row["service_description"],
        "service_date": format_ru_date(row["service_date"]).replace("/", ".") if row["service_date"] else "",
        "service_time": row["service_time"] or "",
        "total_amount": f"{format_money(row['total_amount'])} ₽",
        "prepayment_terms": row["prepayment_terms"] or "",
        "prepayment_amount": f"{format_money(row['prepayment_amount'])} ₽" if row["prepayment_amount"] else "",
    }
    try:
        docx_bytes = _build_trip_contract_docx(data)
    except ImportError:
        return (
            "Формирование договора временно недоступно: на сервере не установлена "
            "библиотека python-docx. Установите зависимости из requirements.txt "
            "и перезапустите приложение.",
            503,
        )
    except FileNotFoundError:
        return (
            "Не найден шаблон договора (static/contract_template.docx) — "
            "загрузите файл шаблона на сервер.",
            503,
        )
    response = app.response_class(
        docx_bytes,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    response.headers["Content-Disposition"] = (
        f'attachment; filename="Dogovor-{row["contract_number"]}.docx"'
    )
    return response


# =======================================================================
# Флот
# =======================================================================
app.register_blueprint(
    create_fleet_blueprint(
        get_db,
        admin_login_required,
        task_assigned_notifier=_notify_task_assignment,
    )
)


# =======================================================================
# Сотрудники
# =======================================================================
app.register_blueprint(
    create_employees_blueprint(
        get_db=get_db,
        admin_login_required=admin_login_required,
        telegram_contacts_fetcher=lambda: fetch_recent_telegram_contacts(TELEGRAM_BOT_TOKEN),
        telegram_sender=lambda chat_id, text: send_telegram_notification(text, chat_id=chat_id),
        telegram_configured=lambda: bool(TELEGRAM_BOT_TOKEN),
        telegram_bot_username=lambda: TELEGRAM_BOT_USERNAME,
    )
)


# =======================================================================
# Экскурсионные услуги
# =======================================================================
app.register_blueprint(
    create_excursion_services_blueprint(
        get_db=get_db,
        access_required=excursion_manager_or_admin_required,
        is_manager_view=_is_customer_manager,
        boats=BOATS,
    )
)


# =======================================================================
# Внутреннее расписание рейсов
# =======================================================================
app.register_blueprint(
    create_schedule_blueprint(
        get_db=get_db,
        access_required=excursion_manager_or_admin_required,
        is_manager_view=_is_customer_manager,
        boats=BOATS,
        boat_colors=BOAT_COLORS,
        avatar_url=find_avatar_url,
        tripster_fetcher=lambda updated_after=None: fetch_tripster_orders(
            TRIPSTER_API_TOKEN, updated_after=updated_after
        ),
        tripster_configured=lambda: bool(TRIPSTER_API_TOKEN),
        cron_secret=CRON_SECRET,
    )
)


# =======================================================================
# Настройки и заявки сотрудников на доработку системы
# =======================================================================
app.register_blueprint(
    create_software_requests_blueprint(
        get_db=get_db,
        admin_login_required=admin_login_required,
        active_team_account=_active_team_account,
    )
)


# =======================================================================
# AI-помощник: OpenAI Responses API + role-aware read-only business tools
# =======================================================================
app.register_blueprint(
    create_ai_assistant_blueprint(
        get_db=get_db,
        current_user=_current_ai_user,
        responses_client=_openai_responses_client,
        model_provider=lambda: OPENAI_AGENT_MODEL,
        boats=BOATS,
        max_concurrent_requests=OPENAI_AGENT_MAX_CONCURRENT,
        requests_per_minute=OPENAI_AGENT_REQUESTS_PER_MINUTE,
    )
)


# =======================================================================
# Тюнинг-центр
# =======================================================================

def _external_order_date(value, fallback):
    """Extract an ISO business date from an integration timestamp."""
    raw = str(value or "").strip()
    if raw:
        try:
            return dt.date.fromisoformat(raw[:10]).isoformat()
        except ValueError:
            try:
                return dt.datetime.fromisoformat(
                    raw.replace("Z", "+00:00")
                ).date().isoformat()
            except ValueError:
                pass
    return fallback


def _order_business_date(order):
    """Read the UI date, with a compatibility fallback for legacy payloads."""
    try:
        value = order["order_date"]
    except (KeyError, IndexError):
        value = None
    if value:
        return str(value)[:10]
    return str(order["created_at"])[:10]


def _process_tuning_form(form, default_order_date=None):
    """Validate a tuning-center order form and compute derived amounts.
    Returns (errors, data). data is None if there are errors."""
    errors = []

    client_name = form.get("client_name", "").strip()
    equipment_type = form.get("equipment_type", "boat").strip()
    if equipment_type not in {item["value"] for item in EQUIPMENT_TYPES}:
        equipment_type = "boat"
    boat_model = form.get("boat_model", "").strip()
    boat_registration_number = form.get("boat_registration_number", "").strip()
    motor_model = form.get("motor_model", "").strip()
    motor_serial_number = form.get("motor_serial_number", "").strip()
    if equipment_type == "motor":
        # A motor-only order must never create or join a boat profile even
        # if a stale hidden boat input was submitted by the browser.
        boat_model = ""
        boat_registration_number = ""
    else:
        # A serial number belongs to a motor-only order. A boat order may
        # still name its installed motor, but that does not make the motor
        # a separate catalog entity or carry over a stale serial number.
        motor_serial_number = ""
    phone = form.get("phone", "").strip()
    raw_client_id = form.get("client_id", "").strip()
    order_date_raw = form.get("order_date", "").strip()
    order_date = default_order_date or dt.date.today().isoformat()
    if order_date_raw:
        try:
            order_date = dt.date.fromisoformat(order_date_raw).isoformat()
        except ValueError:
            errors.append("Укажите корректную дату заказа.")
    sale_channel = form.get("sale_channel", "direct").strip()
    if sale_channel not in [c["value"] for c in SALE_CHANNELS]:
        sale_channel = "direct"

    if not client_name:
        errors.append("Укажите ФИО клиента.")
    if equipment_type == "boat" and not boat_model:
        errors.append("Укажите модель лодки.")
    if equipment_type == "motor" and not motor_model:
        errors.append("Укажите модель мотора.")

    discount_type = form.get("discount_type", "percent").strip()
    if discount_type not in ("percent", "amount"):
        discount_type = "percent"
    discount_raw = form.get("discount_value", "").strip().replace(",", ".")
    discount_value = 0.0
    if discount_raw:
        try:
            discount_value = float(discount_raw)
            if discount_value < 0:
                errors.append("Скидка не может быть отрицательной.")
            elif discount_type == "percent" and discount_value > 100:
                errors.append("Скидка в процентах не может быть больше 100.")
        except ValueError:
            errors.append("Скидка должна быть числом.")

    names = form.getlist("work_name[]")
    costs = form.getlist("cost_price[]")
    mults = form.getlist("multiplier[]")
    item_ids = form.getlist("item_id[]")

    def _get(lst, i):
        return lst[i] if i < len(lst) else ""

    items = []
    subtotal = 0.0
    for i in range(len(names)):
        name = names[i].strip()
        cost_raw = _get(costs, i).strip().replace(",", ".")
        mult_raw = _get(mults, i).strip().replace(",", ".")
        if not name and not cost_raw and not mult_raw:
            continue  # полностью пустая строка — пропускаем

        row_num = i + 1
        if not name:
            errors.append(f"Работа №{row_num}: не указано название.")

        cost = mult = None
        try:
            cost = float(cost_raw)
            if cost < 0:
                errors.append(f"Работа №{row_num}: себестоимость не может быть отрицательной.")
        except ValueError:
            errors.append(f"Работа №{row_num}: себестоимость должна быть числом.")
        try:
            mult = float(mult_raw)
            if mult < 0:
                errors.append(f"Работа №{row_num}: коэффициент не может быть отрицательным.")
        except ValueError:
            errors.append(f"Работа №{row_num}: коэффициент должен быть числом.")

        if name and cost is not None and mult is not None:
            price = cost * mult
            item_id_raw = _get(item_ids, i).strip()
            items.append({
                "item_id": int(item_id_raw) if item_id_raw.isdigit() else None,
                "work_name": name, "cost_price": cost, "multiplier": mult, "price": price,
            })
            subtotal += price

    if not items and not any("Работа" in e for e in errors):
        errors.append("Добавьте хотя бы одну работу.")

    if discount_type == "amount" and discount_value > subtotal:
        errors.append("Скидка суммой не может быть больше суммы работ.")

    if errors:
        return errors, None

    if discount_type == "percent":
        discount_amount = subtotal * discount_value / 100
    else:
        discount_amount = discount_value
    total = subtotal - discount_amount

    data = dict(
        client_id=int(raw_client_id) if raw_client_id.isdigit() else None,
        client_name=client_name, equipment_type=equipment_type,
        boat_model=boat_model, boat_registration_number=boat_registration_number,
        motor_model=motor_model, motor_serial_number=motor_serial_number, phone=phone,
        order_date=order_date,
        sale_channel=sale_channel, discount_type=discount_type, discount_value=discount_value,
        # discount_pct is kept only for older code/rows that still read it —
        # 0 when the discount is a fixed amount, since it isn't a percent.
        discount_pct=discount_value if discount_type == "percent" else 0.0,
        items=items, subtotal=subtotal, discount_amount=discount_amount, total=total,
    )
    return errors, data


def _get_or_create_client(db, phone, client_name, boat_model, client_id=None):
    """Resolve a selected client, then an unambiguous phone, or create one."""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    row = None
    if client_id is not None:
        row = db.execute(
            "SELECT id, boat_model, phone FROM clients WHERE id = ?", (client_id,)
        ).fetchone()
    if row is None and phone:
        matches = db.execute(
            "SELECT id, boat_model, phone, client_name FROM clients WHERE phone = ? "
            "ORDER BY id",
            (phone,),
        ).fetchall()
        if len(matches) == 1:
            row = matches[0]
        elif len(matches) > 1:
            named = [match for match in matches if match["client_name"] == client_name]
            if len(named) == 1:
                row = named[0]
    if row:
        db.execute(
            "UPDATE clients SET client_name = ?, boat_model = ?, phone = ? WHERE id = ?",
            (
                client_name, boat_model or row["boat_model"],
                phone or row["phone"], row["id"],
            ),
        )
        ensure_client_segment(db, row["id"], TUNING_SEGMENT, now)
        return row["id"]
    token = secrets.token_urlsafe(16)
    cur = db.execute(
        "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (client_name, boat_model, phone, token, now),
    )
    ensure_client_segment(db, cur.lastrowid, TUNING_SEGMENT, now)
    return cur.lastrowid


def _order_payment_totals(db, order_id, total):
    payments = db.execute(
        "SELECT tuning_payments.*, "
        "modulkassa_receipts.status AS receipt_status, "
        "modulkassa_receipts.failure_message AS receipt_failure_message "
        "FROM tuning_payments "
        # A retry adds another modulkassa_receipts row for the same
        # payment rather than overwriting the old one (keeps history of
        # every attempt) — join only the latest one, or a plain join would
        # multiply the payment row per attempt and double-count its amount
        # in paid_amount below.
        "LEFT JOIN modulkassa_receipts ON modulkassa_receipts.id = ("
        "  SELECT id FROM modulkassa_receipts mr2"
        "  WHERE mr2.payment_id = tuning_payments.id ORDER BY mr2.id DESC LIMIT 1"
        ") "
        "WHERE tuning_payments.order_id = ? "
        "ORDER BY tuning_payments.paid_at DESC, tuning_payments.id DESC",
        (order_id,),
    ).fetchall()
    paid_amount = sum(p["amount"] for p in payments)
    remaining = max(0.0, total - paid_amount)
    return payments, paid_amount, remaining


def _modulkassa_configured():
    return bool(MODULKASSA_USERNAME and MODULKASSA_PASSWORD)


def _modulkassa_contact_from_phone(phone):
    """ModulKassa's "email" field also accepts a phone number, required in
    the shape +7<10 digits> or 8<10 digits> — reformat whatever we have on
    the order into that, falling back to the raw value if it doesn't look
    like a normal 11-digit RU number."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return phone or ""


def _modulkassa_fiscalize_payment(db, order, payment_id, amount, payment_type):
    """Best-effort — sends one order payment to ModulKassa as a single
    "Оплата по заказу №..." line (see chat: client pays for the whole
    project, not itemized work, so a lump line is what's meaningful here).
    Fiscalization itself happens asynchronously on the merchant's own
    till (it polls ModulKassa's server every 5s), so this only queues the
    document and records a modulkassa_receipts row — status is filled in
    later by _modulkassa_check_status, via the cron endpoint or a manual
    check. Never raises: a ModulKassa outage must not stop the payment
    itself from being recorded."""
    if not _modulkassa_configured():
        return
    doc_id = str(uuid.uuid4())
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = {
        "id": doc_id,
        "docNum": f"order-{order['id']}-payment-{payment_id}",
        "docType": "SALE",
        "checkoutDateTime": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "email": _modulkassa_contact_from_phone(order["phone"]),
        "printReceipt": False,
        "taxMode": None,
        "inventPositions": [{
            "name": f"Оплата по заказу №{order['id']}",
            "price": amount,
            "quantity": 1,
            "vatTag": MODULKASSA_VAT_TAG,
            "paymentObject": "service",
            "paymentMethod": "full_payment",
        }],
        "moneyPositions": [{"paymentType": payment_type, "sum": amount}],
    }
    try:
        resp = requests.post(
            f"{MODULKASSA_BASE_URL}/v2/doc", json=body,
            auth=(MODULKASSA_USERNAME, MODULKASSA_PASSWORD), timeout=15,
        )
    except requests.RequestException as e:
        db.execute(
            "INSERT INTO modulkassa_receipts (payment_id, doc_id, status, failure_message, created_at, updated_at) "
            "VALUES (?, ?, 'failed', ?, ?, ?)",
            (payment_id, doc_id, str(e), now, now),
        )
        db.commit()
        return
    if resp.ok:
        status = (resp.json().get("status") or "queued").lower()
        db.execute(
            "INSERT INTO modulkassa_receipts (payment_id, doc_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (payment_id, doc_id, status, now, now),
        )
    else:
        db.execute(
            "INSERT INTO modulkassa_receipts (payment_id, doc_id, status, failure_message, created_at, updated_at) "
            "VALUES (?, ?, 'failed', ?, ?, ?)",
            (payment_id, doc_id, f"HTTP {resp.status_code}: {resp.text[:300]}", now, now),
        )
    db.commit()


# Statuses ModulKassa returns (see /v1/doc/<id>/status) — QUEUED and
# PENDING are still in flight (till hasn't picked it up / is printing);
# PRINTED/WAIT_FOR_CALLBACK/COMPLETED all mean the fiscal receipt itself
# was successfully issued (WAIT_FOR_CALLBACK just means our unused
# responseURL callback didn't get a 200 back, not that the receipt
# failed); FAILED needs a manual retry. Reuses the existing status-badge
# CSS classes (status-pending/status-done/status-cancelled) instead of
# adding new ones.
MODULKASSA_STATUS_DISPLAY = {
    "queued": ("В очереди", "pending"),
    "pending": ("Печатается на кассе", "pending"),
    "printed": ("Пробит", "done"),
    "wait_for_callback": ("Пробит", "done"),
    "completed": ("Пробит", "done"),
    "failed": ("Ошибка", "cancelled"),
}


def _modulkassa_check_status(db, receipt):
    """Polls one receipt's current status and updates the row. Best-effort
    — a network hiccup here just leaves the row as it was, retried on the
    next cron pass or manual check."""
    try:
        resp = requests.get(
            f"{MODULKASSA_BASE_URL}/v1/doc/{receipt['doc_id']}/status",
            auth=(MODULKASSA_USERNAME, MODULKASSA_PASSWORD), timeout=15,
        )
    except requests.RequestException:
        return
    if not resp.ok:
        return
    data = resp.json()
    status = (data.get("status") or "").lower()
    if not status:
        return
    failure_info = data.get("failureInfo")
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE modulkassa_receipts SET status=?, fiscal_info_json=?, failure_message=?, updated_at=? WHERE id=?",
        (
            status,
            json.dumps(data.get("fiscalInfo"), ensure_ascii=False) if data.get("fiscalInfo") else None,
            json.dumps(failure_info, ensure_ascii=False) if failure_info else None,
            now, receipt["id"],
        ),
    )


def _order_notes(db, order_id):
    notes = [
        dict(n) for n in db.execute(
            "SELECT n.*, a.admin_name AS author_name "
            "FROM tuning_order_notes n LEFT JOIN admin_accounts a ON a.id = n.author_admin_id "
            "WHERE n.order_id = ? ORDER BY n.created_at DESC, n.id DESC",
            (order_id,),
        ).fetchall()
    ]
    for note in notes:
        note["reminders"] = db.execute(
            "SELECT r.*, e.name AS remind_employee_name, "
            "a.admin_name AS remind_admin_name, "
            "COALESCE(e.name, a.admin_name) AS remind_recipient_name "
            "FROM tuning_order_note_reminders r "
            "LEFT JOIN employees e ON e.id = r.remind_employee_id "
            "LEFT JOIN admin_accounts a ON a.id = r.remind_admin_id "
            "WHERE r.note_id = ? ORDER BY r.remind_at",
            (note["id"],),
        ).fetchall()
    return notes


def _note_reminder_recipients(db):
    """Active employees plus legacy administrator reminder recipients."""
    recipients = []
    for row in db.execute(
        "SELECT e.id, e.name, "
        "COALESCE((SELECT GROUP_CONCAT(ep.position, ' · ') "
        " FROM employee_positions ep WHERE ep.employee_id = e.id), '') AS positions, "
        "eta.chat_id, eta.username "
        "FROM employees e "
        "LEFT JOIN employee_telegram_accounts eta ON eta.employee_id = e.id "
        "WHERE e.deleted_at IS NULL ORDER BY e.name COLLATE NOCASE"
    ).fetchall():
        recipients.append({
            "value": f"employee:{row['id']}",
            "name": row["name"],
            "subtitle": row["positions"] or "Сотрудник",
            "telegram_linked": bool(row["chat_id"]),
            "telegram_username": row["username"] or "",
            "kind": "employee",
        })
    # Keep administrator accounts in the same picker so the former
    # self-reminder workflow and existing Telegram links remain available.
    for row in db.execute(
        "SELECT id, admin_name, telegram_chat_id FROM admin_accounts "
        "ORDER BY admin_name COLLATE NOCASE"
    ).fetchall():
        recipients.append({
            "value": f"admin:{row['id']}",
            "name": row["admin_name"],
            "subtitle": "Администратор",
            "telegram_linked": bool(row["telegram_chat_id"]),
            "telegram_username": "",
            "kind": "admin",
        })
    recipients.sort(key=lambda recipient: (
        recipient["name"].casefold(), recipient["kind"] != "employee"
    ))
    return recipients


def _recompute_order_totals(db, order_id):
    """Work items (tuning_order_items) and goods (tuning_order_products) are
    added/removed through separate forms/routes, so neither one alone can
    keep tuning_orders.subtotal/total right — this re-derives both from
    scratch (work price sum + goods qty*unit_price sum) against whatever
    discount_type/discount_value is currently stored on the order, and
    writes the result back. Call after any change to either line-item set,
    or to the discount itself."""
    order = db.execute(
        "SELECT discount_type, discount_value FROM tuning_orders WHERE id = ?", (order_id,)
    ).fetchone()
    # A "Задача снята" work item stays in the table (for the record) but no
    # longer counts toward what the client owes.
    work_subtotal = db.execute(
        "SELECT COALESCE(SUM(price), 0) AS s FROM tuning_order_items WHERE order_id = ? AND status != 'removed'",
        (order_id,),
    ).fetchone()["s"]
    goods_subtotal = db.execute(
        "SELECT COALESCE(SUM(quantity * unit_price), 0) AS s FROM tuning_order_products WHERE order_id = ?",
        (order_id,),
    ).fetchone()["s"]
    subtotal = work_subtotal + goods_subtotal
    if order["discount_type"] == "amount":
        discount_amount = min(order["discount_value"], subtotal)
    else:
        discount_amount = subtotal * order["discount_value"] / 100
    total = subtotal - discount_amount
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "UPDATE tuning_orders SET subtotal = ?, total = ?, updated_at = ? WHERE id = ?",
        (subtotal, total, now, order_id),
    )
    db.commit()


# ---------------------------------------------------------------------
# Акт выполненных работ (PDF)
# ---------------------------------------------------------------------

_ONES_M = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_ONES_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят",
         "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот",
             "восемьсот", "девятьсот"]


def _plural_ru(n, forms):
    """forms = (для 1, для 2-4, для 5+/11-14) e.g. ('рубль', 'рубля', 'рублей')."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]


def _three_digits_ru(n, feminine=False):
    words = []
    hundreds, rest = divmod(n, 100)
    if hundreds:
        words.append(_HUNDREDS[hundreds])
    if 10 <= rest < 20:
        words.append(_TEENS[rest - 10])
    else:
        tens, ones = divmod(rest, 10)
        if tens:
            words.append(_TENS[tens])
        if ones:
            words.append((_ONES_F if feminine else _ONES_M)[ones])
    return words


def _rubles_to_words(amount):
    """1234.5 -> 'Одна тысяча двести тридцать четыре рубля 50 копеек'."""
    amount = round(float(amount), 2)
    rub = int(amount)
    kop = int(round((amount - rub) * 100))

    thousands, rest = divmod(rub, 1000)
    words = []
    if thousands:
        words += _three_digits_ru(thousands, feminine=True)
        words.append(_plural_ru(thousands, ("тысяча", "тысячи", "тысяч")))
    if rest or not words:
        words += _three_digits_ru(rest, feminine=False)
    if not words:
        words.append("ноль")
    words.append(_plural_ru(rub, ("рубль", "рубля", "рублей")))

    sentence = " ".join(words)
    sentence = sentence[0].upper() + sentence[1:]
    return f"{sentence} {kop:02d} {_plural_ru(kop, ('копейка', 'копейки', 'копеек'))}"


_ACT_FONTS_REGISTERED = False


def _register_act_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    global _ACT_FONTS_REGISTERED
    if _ACT_FONTS_REGISTERED:
        return
    fonts_dir = os.path.join(app.static_folder, "fonts")
    pdfmetrics.registerFont(TTFont("OpenSans", os.path.join(fonts_dir, "open-sans-400.ttf")))
    pdfmetrics.registerFont(TTFont("OpenSans-Bold", os.path.join(fonts_dir, "open-sans-700.ttf")))
    _ACT_FONTS_REGISTERED = True


COMPANY_NAME = 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "БОДРЫЙ БОЦМАН"'
COMPANY_ADDRESS = "197762, Россия, г Санкт-Петербург, г Кронштадт, ул Мануильского, 20 литера а, 2"
# Search context, coordinates and zoom from the shared Yandex URL are omitted:
# the organisation id and add-review flag identify the same target while keeping
# the printed QR substantially less dense and easier for phones to scan.
TUNING_REVIEW_URL = (
    "https://yandex.ru/maps/org/bodry_botsman/15778336383/reviews/"
    "?add-review=true"
)


def _build_tuning_review_qr(size):
    """Create a vector QR so it remains sharp in printed acts and PDFs."""
    from reportlab.graphics.barcode import qr
    from reportlab.graphics.shapes import Drawing

    qr_code = qr.QrCodeWidget(
        TUNING_REVIEW_URL,
        barWidth=size,
        barHeight=size,
        barBorder=1,
    )
    drawing = Drawing(size, size)
    drawing.add(qr_code)
    return drawing


def _build_act_pdf(order, items, goods=()):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    _register_act_fonts()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
    )

    style_company = ParagraphStyle("company", fontName="OpenSans-Bold", fontSize=10.5, leading=14)
    style_address = ParagraphStyle("address", fontName="OpenSans-Bold", fontSize=10.5, leading=14,
                                    spaceBefore=2, spaceAfter=16)
    style_title = ParagraphStyle("title", fontName="OpenSans-Bold", fontSize=22, leading=26,
                                  alignment=TA_CENTER, spaceAfter=6)
    style_subtitle = ParagraphStyle("subtitle", fontName="OpenSans-Bold", fontSize=14, leading=18,
                                     alignment=TA_CENTER, spaceAfter=22)
    style_client = ParagraphStyle("client", fontName="OpenSans", fontSize=10.5, leading=14)
    style_cell = ParagraphStyle("cell", fontName="OpenSans", fontSize=9.5, leading=12.5)
    style_bold = ParagraphStyle("bold", fontName="OpenSans-Bold", fontSize=10.5, leading=15,
                                 spaceAfter=4)
    style_section = ParagraphStyle("section", fontName="OpenSans-Bold", fontSize=11.5, leading=15,
                                    spaceBefore=4, spaceAfter=8)
    style_review = ParagraphStyle("review", fontName="OpenSans", fontSize=10, leading=14)

    try:
        order_date = dt.date.fromisoformat(_order_business_date(order)).strftime("%d.%m.%Y")
    except ValueError:
        order_date = _order_business_date(order)

    flow = []
    logo_path = os.path.join(app.static_folder, "logo-act.png")
    logo_w = 130
    flow.append(Image(logo_path, width=logo_w, height=logo_w * 230 / 836))
    flow.append(Spacer(1, 12))
    flow.append(Paragraph(f"<u>{COMPANY_NAME}</u>", style_company))
    flow.append(Paragraph(COMPANY_ADDRESS, style_address))
    flow.append(Paragraph("Акт выполненных работ", style_title))
    flow.append(Paragraph(f"По заказу № {order['id']} от {order_date}", style_subtitle))

    flow.append(Paragraph(
        f"Заказчик {order['client_name']} ({tuning_equipment_label(order)})",
        style_client,
    ))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.black,
                            spaceBefore=2, spaceAfter=18))

    col_widths = [12 * mm, 76 * mm, 24 * mm, 18 * mm, 18 * mm, 22 * mm]
    table_style = TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "OpenSans"),
        ("FONTNAME", (0, 0), (-1, 0), "OpenSans-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "OpenSans-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (2, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ])

    # Two separate sections/tables (Работы / Товары) instead of one mixed
    # list — only headed when both are actually present, so a pure-work
    # order's act still renders exactly as it always has.
    total_sum = 0.0
    if goods:
        flow.append(Paragraph("Работы", style_section))
    table_data = [["№", "Наименование работы", "Цена", "Кол-во", "Ед. изм.", "Сумма"]]
    for i, item in enumerate(items, start=1):
        price_str = f"{item['price']:.2f}".replace(".", ",")
        table_data.append([
            str(i), Paragraph(item["work_name"], style_cell),
            price_str, "1", "шт", price_str,
        ])
        total_sum += item["price"]
    table_data.append([
        "", "", "", f"{len(items):.2f}".replace(".", ","), "Итого:",
        f"{total_sum:.2f}".replace(".", ","),
    ])
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(table_style)
    flow.append(tbl)

    if goods:
        flow.append(Spacer(1, 18))
        flow.append(Paragraph("Товары", style_section))
        goods_data = [["№", "Наименование товара", "Цена", "Кол-во", "Ед. изм.", "Сумма"]]
        goods_sum = 0.0
        goods_qty = 0.0
        for i, g in enumerate(goods, start=1):
            price_str = f"{g['unit_price']:.2f}".replace(".", ",")
            line_sum = g["quantity"] * g["unit_price"]
            unit_label = next((u["label"] for u in SUPPLY_COST_UNITS if u["value"] == g["unit"]), g["unit"])
            goods_data.append([
                str(i), Paragraph(g["product_name"], style_cell), price_str,
                f"{g['quantity']:g}", unit_label, f"{line_sum:.2f}".replace(".", ","),
            ])
            goods_sum += line_sum
            goods_qty += g["quantity"]
        goods_data.append([
            "", "", "", f"{goods_qty:g}", "Итого:",
            f"{goods_sum:.2f}".replace(".", ","),
        ])
        goods_tbl = Table(goods_data, colWidths=col_widths, repeatRows=1)
        goods_tbl.setStyle(table_style)
        flow.append(goods_tbl)
        total_sum += goods_sum

    flow.append(Spacer(1, 22))

    flow.append(Paragraph(
        f"Итого выполнено работ и продано товаров на сумму: {_rubles_to_words(total_sum)}", style_bold,
    ))
    flow.append(Spacer(1, 14))
    flow.append(Paragraph("Работы выполнены качественно и в срок, полностью оплачены", style_bold))
    flow.append(Paragraph("Стороны претензий друг к другу не имеют", style_bold))

    review_qr_size = 30 * mm
    review_table = Table(
        [[
            _build_tuning_review_qr(review_qr_size),
            Paragraph(
                "<b>Понравилась наша работа?</b><br/>"
                "Отсканируйте QR-код и оставьте отзыв о тюнинг-центре "
                "«Бодрый Боцман» на Яндекс Картах.",
                style_review,
            ),
        ]],
        colWidths=[36 * mm, 134 * mm],
    )
    review_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (0, 0), 6),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(Spacer(1, 18))
    flow.append(review_table)
    flow.append(Spacer(1, 34))

    sig_table = Table(
        [["Заказчик " + "_" * 28, "Исполнитель" + "_" * 24]],
        colWidths=[85 * mm, 85 * mm],
    )
    sig_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "OpenSans-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(sig_table)

    doc.build(flow)
    return buf.getvalue()


@app.route("/tuning/<int:order_id>/act.pdf")
@admin_login_required
def tuning_order_act_pdf(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))
    items = db.execute(
        "SELECT * FROM tuning_order_items WHERE order_id = ? AND status = 'done' ORDER BY id",
        (order_id,),
    ).fetchall()
    goods = db.execute(
        "SELECT * FROM tuning_order_products WHERE order_id = ? ORDER BY id", (order_id,)
    ).fetchall()
    try:
        pdf_bytes = _build_act_pdf(order, items, goods)
    except ImportError:
        return (
            "Формирование PDF временно недоступно: на сервере не установлена "
            "библиотека reportlab. Установите зависимости из requirements.txt "
            "и перезапустите приложение.",
            503,
        )
    response = app.response_class(pdf_bytes, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'inline; filename="Akt-{order_id}.pdf"'
    return response


def _build_handover_act_pdf(order, items, goods=()):
    """Same layout as _build_act_pdf (completed-work act), but signed when
    the customer drops the boat off — before any work is necessarily done,
    so it lists every work item in the order regardless of status, and
    closes with a handover statement instead of a quality/payment one."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    _register_act_fonts()
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
    )

    style_company = ParagraphStyle("company", fontName="OpenSans-Bold", fontSize=10.5, leading=14)
    style_address = ParagraphStyle("address", fontName="OpenSans-Bold", fontSize=10.5, leading=14,
                                    spaceBefore=2, spaceAfter=16)
    style_title = ParagraphStyle("title", fontName="OpenSans-Bold", fontSize=22, leading=26,
                                  alignment=TA_CENTER, spaceAfter=6)
    style_subtitle = ParagraphStyle("subtitle", fontName="OpenSans-Bold", fontSize=14, leading=18,
                                     alignment=TA_CENTER, spaceAfter=22)
    style_client = ParagraphStyle("client", fontName="OpenSans", fontSize=10.5, leading=14)
    style_cell = ParagraphStyle("cell", fontName="OpenSans", fontSize=9.5, leading=12.5)
    style_bold = ParagraphStyle("bold", fontName="OpenSans-Bold", fontSize=10.5, leading=15,
                                 spaceAfter=4)
    style_section = ParagraphStyle("section", fontName="OpenSans-Bold", fontSize=11.5, leading=15,
                                    spaceBefore=4, spaceAfter=8)

    try:
        order_date = dt.date.fromisoformat(_order_business_date(order)).strftime("%d.%m.%Y")
    except ValueError:
        order_date = _order_business_date(order)

    flow = []
    logo_path = os.path.join(app.static_folder, "logo-act.png")
    logo_w = 130
    flow.append(Image(logo_path, width=logo_w, height=logo_w * 230 / 836))
    flow.append(Spacer(1, 12))
    flow.append(Paragraph(f"<u>{COMPANY_NAME}</u>", style_company))
    flow.append(Paragraph(COMPANY_ADDRESS, style_address))
    flow.append(Paragraph("Акт приёма-передачи", style_title))
    flow.append(Paragraph(f"По заказу № {order['id']} от {order_date}", style_subtitle))

    flow.append(Paragraph(
        f"Заказчик {order['client_name']} ({tuning_equipment_label(order)})",
        style_client,
    ))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.black,
                            spaceBefore=2, spaceAfter=18))

    col_widths = [12 * mm, 76 * mm, 24 * mm, 18 * mm, 18 * mm, 22 * mm]
    table_style = TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "OpenSans"),
        ("FONTNAME", (0, 0), (-1, 0), "OpenSans-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "OpenSans-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (2, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ])

    # Two separate sections/tables (Работы / Товары) instead of one mixed
    # list — only headed when both are actually present, so a pure-work
    # order's act still renders exactly as it always has.
    total_sum = 0.0
    if goods:
        flow.append(Paragraph("Работы", style_section))
    table_data = [["№", "Наименование работы", "Цена", "Кол-во", "Ед. изм.", "Сумма"]]
    for i, item in enumerate(items, start=1):
        price_str = f"{item['price']:.2f}".replace(".", ",")
        table_data.append([
            str(i), Paragraph(item["work_name"], style_cell),
            price_str, "1", "шт", price_str,
        ])
        total_sum += item["price"]
    table_data.append([
        "", "", "", f"{len(items):.2f}".replace(".", ","), "Итого:",
        f"{total_sum:.2f}".replace(".", ","),
    ])
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(table_style)
    flow.append(tbl)

    if goods:
        flow.append(Spacer(1, 18))
        flow.append(Paragraph("Товары", style_section))
        goods_data = [["№", "Наименование товара", "Цена", "Кол-во", "Ед. изм.", "Сумма"]]
        goods_sum = 0.0
        goods_qty = 0.0
        for i, g in enumerate(goods, start=1):
            price_str = f"{g['unit_price']:.2f}".replace(".", ",")
            line_sum = g["quantity"] * g["unit_price"]
            unit_label = next((u["label"] for u in SUPPLY_COST_UNITS if u["value"] == g["unit"]), g["unit"])
            goods_data.append([
                str(i), Paragraph(g["product_name"], style_cell), price_str,
                f"{g['quantity']:g}", unit_label, f"{line_sum:.2f}".replace(".", ","),
            ])
            goods_sum += line_sum
            goods_qty += g["quantity"]
        goods_data.append([
            "", "", "", f"{goods_qty:g}", "Итого:",
            f"{goods_sum:.2f}".replace(".", ","),
        ])
        goods_tbl = Table(goods_data, colWidths=col_widths, repeatRows=1)
        goods_tbl.setStyle(table_style)
        flow.append(goods_tbl)
        total_sum += goods_sum

    if order["discount_type"] == "amount":
        discount_amount = order["discount_value"]
    else:
        discount_amount = total_sum * order["discount_value"] / 100

    # Only when there's something to clarify beyond the section table(s)'
    # own "Итого:" rows — a plain no-discount, no-goods order needs no
    # extra summary line, same as before goods existed at all.
    if discount_amount > 0 or goods:
        summary_data = []
        if discount_amount > 0:
            discount_label = (
                f"Скидка ({('%g' % order['discount_value']).replace('.', ',')}%):"
                if order["discount_type"] != "amount"
                else "Скидка:"
            )
            summary_data.append([
                "", "", "", "", discount_label,
                f"{discount_amount:.2f}".replace(".", ","),
            ])
        summary_data.append([
            "", "", "", "", "К оплате:",
            f"{total_sum - discount_amount:.2f}".replace(".", ","),
        ])
        summary_tbl = Table(summary_data, colWidths=col_widths)
        summary_tbl.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "OpenSans-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        flow.append(summary_tbl)

    flow.append(Spacer(1, 22))

    flow.append(Paragraph("Заказчик лодку/мотор передал, а Исполнитель принял", style_bold))
    flow.append(Spacer(1, 46))

    sig_table = Table(
        [["Заказчик " + "_" * 28, "Исполнитель" + "_" * 24]],
        colWidths=[85 * mm, 85 * mm],
    )
    sig_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "OpenSans-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow.append(sig_table)

    doc.build(flow)
    return buf.getvalue()


CONTRACT_TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "contract_template.docx")


def _build_trip_contract_docx(data):
    """Fill static/contract_template.docx ("Договор оказания услуг") with
    a contract's data and return the resulting .docx bytes.

    Edits the template's existing paragraph runs in place (via python-docx)
    rather than building a document from scratch, so every bit of the
    original formatting — fonts, tab stops, the numbered-list clauses —
    survives untouched; only the run objects holding blanks change.

    The (paragraph index, run index) pairs below were hand-mapped against
    the exact current static/contract_template.docx and verified by
    rendering a filled sample and reading it back — if that template file
    is ever replaced with a differently-worded one, this map has to be
    redone against the new file's paragraph/run structure, the same way.
    An optional field whose value is empty is left untouched, so its
    blank stays a visible run of underscores for hand-completion instead
    of silently asserting an empty string."""
    import docx

    doc = docx.Document(CONTRACT_TEMPLATE_PATH)
    paras = doc.paragraphs

    # P0: "Договор оказания услуг 200825-1" — replace just the number.
    paras[0].runs[0].text = paras[0].runs[0].text.replace("200825-1", data["contract_number"])

    # P1: "г. Санкт-Петербург [tab-aligned]20.08.2025" — date is the last run.
    paras[1].runs[-1].text = data["contract_date"]

    # P2: preamble — Заказчик name/representative/basis, and the
    # Исполнитель director's name, which the template itself has wrong
    # here (right in the signature block at P36) — always corrected.
    p2 = paras[2]
    p2.runs[0].text = data["client_name"]
    if data["client_representative"]:
        p2.runs[4].text = data["client_representative"]
    if data["client_representative_basis"]:
        p2.runs[6].text = data["client_representative_basis"]
    p2.runs[13].text = p2.runs[13].text.replace("Евгений Аленович", "Даниил Евгеньевич")

    # P4: "...следующую услугу (далее- Услуга): ______."
    paras[4].runs[3].text = data["service_description"]

    # P5/P6: "Дата/Время оказания услуги: ______" — label and blank share
    # one run, so append after the label rather than replacing it whole.
    if data["service_date"]:
        paras[5].runs[0].text = "Дата оказания услуги: " + data["service_date"]
    if data["service_time"]:
        paras[6].runs[0].text = "Время оказания услуги: " + data["service_time"]

    # P10: cost/prepayment clause.
    p10 = paras[10]
    p10.runs[2].text = data["total_amount"]
    if data["prepayment_terms"]:
        p10.runs[8].text = data["prepayment_terms"]
    if data["prepayment_amount"]:
        p10.runs[15].text = data["prepayment_amount"]

    # P29: "Заказчик: ______" in the signature block — full requisites if
    # given, otherwise just repeat the name from P2.
    paras[29].runs[2].text = data["client_requisites"] or data["client_name"]

    # P30: "От имени Заказчика______ <printed name>" — mirrors how the
    # Исполнитель's own signature line (P36) prints the director's name.
    paras[30].runs[1].text = data["client_representative"] or data["client_name"]

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


@app.route("/tuning/<int:order_id>/handover.pdf")
@admin_login_required
def tuning_order_handover_pdf(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))
    items = db.execute(
        "SELECT * FROM tuning_order_items WHERE order_id = ? AND status != 'removed' ORDER BY id",
        (order_id,),
    ).fetchall()
    goods = db.execute(
        "SELECT * FROM tuning_order_products WHERE order_id = ? ORDER BY id", (order_id,)
    ).fetchall()
    try:
        pdf_bytes = _build_handover_act_pdf(order, items, goods)
    except ImportError:
        return (
            "Формирование PDF временно недоступно: на сервере не установлена "
            "библиотека reportlab. Установите зависимости из requirements.txt "
            "и перезапустите приложение.",
            503,
        )
    response = app.response_class(pdf_bytes, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'inline; filename="Akt-priema-{order_id}.pdf"'
    return response


@app.route("/tuning")
@admin_login_required
def tuning_index():
    db = get_db()
    _sync_tuning_boat_profiles(db)
    db.commit()
    order_rows = db.execute(
        "SELECT * FROM tuning_orders ORDER BY order_date DESC, id DESC"
    ).fetchall()
    profile_ids_by_model = {
        row["model_key"]: row["id"]
        for row in db.execute(
            "SELECT id, model_key FROM tuning_boat_profiles"
        ).fetchall()
    }
    orders = []
    for row in order_rows:
        order = dict(row)
        equipment_type = (
            "motor" if order["equipment_type"] == "motor" else "boat"
        )
        model_name = (
            order["motor_model"] if equipment_type == "motor"
            else order["boat_model"]
        )
        profile_id = profile_ids_by_model.get(
            _tuning_equipment_profile_key(equipment_type, model_name)
        )
        order["equipment_profile_id"] = profile_id
        order["boat_profile_id"] = profile_id if equipment_type == "boat" else None
        order["motor_profile_id"] = profile_id if equipment_type == "motor" else None
        orders.append(order)
    grand_total = sum(o["total"] for o in orders)
    return render_template(
        "tuning_index.html", orders=orders, grand_total=grand_total,
        order_statuses=ORDER_STATUSES,
        active_page="tuning", sub_page="orders",
    )


def _client_pagination_items(current_page, total_pages):
    if total_pages <= 7:
        return list(range(1, total_pages + 1))
    visible = sorted({
        1,
        total_pages,
        max(1, current_page - 1),
        current_page,
        min(total_pages, current_page + 1),
    })
    items = []
    previous = None
    for page_number in visible:
        if previous is not None and page_number - previous > 1:
            items.append(None)
        items.append(page_number)
        previous = page_number
    return items


@app.route("/admin/clients")
@excursion_manager_or_admin_required
def tuning_clients():
    db = get_db()
    manager_view = _is_customer_manager(db)
    if manager_view:
        section = EXCURSION_SEGMENT
    else:
        section = request.args.get("section", TUNING_SEGMENT).strip().lower()
        if section not in (TUNING_SEGMENT, EXCURSION_SEGMENT):
            section = TUNING_SEGMENT

    search_query = " ".join(request.args.get("q", "").split())[:120]
    search_pattern = f"%{search_query.casefold()}%"
    try:
        requested_page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        requested_page = 1

    segment_counts = {
        row["segment"]: row["client_count"]
        for row in db.execute(
            "SELECT client_segments.segment, "
            "COUNT(DISTINCT clients.id) AS client_count "
            "FROM client_segments JOIN clients "
            "ON clients.id = client_segments.client_id "
            "GROUP BY client_segments.segment"
        ).fetchall()
    }

    clients_count = 0
    orders_count = 0
    order_total = 0.0
    outstanding_total = 0.0
    trips_count = 0
    upcoming_count = 0
    tuning_scope_sql = (
        "(EXISTS (SELECT 1 FROM client_segments scope_cs "
        " WHERE scope_cs.client_id = c.id AND scope_cs.segment = ?) "
        "OR NOT EXISTS (SELECT 1 FROM client_segments any_cs "
        " WHERE any_cs.client_id = c.id))"
    )

    if section == EXCURSION_SEGMENT:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        summary = db.execute(
            "SELECT COUNT(DISTINCT c.id) AS clients_count, "
            "COUNT(DISTINCT CASE WHEN si.id IS NOT NULL THEN "
            " CAST(c.id AS TEXT) || ':' || CAST(si.id AS TEXT) END) AS trips_count, "
            "COUNT(DISTINCT CASE WHEN si.starts_at >= ? THEN "
            " CAST(c.id AS TEXT) || ':' || CAST(si.id AS TEXT) END) AS upcoming_count "
            "FROM clients c JOIN client_segments cs ON cs.client_id = c.id "
            "AND cs.segment = ? "
            "LEFT JOIN schedule_participants sp ON sp.client_id = c.id "
            "LEFT JOIN schedule_items si ON si.id = sp.schedule_item_id "
            "AND si.deleted_at IS NULL",
            (now, EXCURSION_SEGMENT),
        ).fetchone()
        clients_count = summary["clients_count"]
        trips_count = summary["trips_count"]
        upcoming_count = summary["upcoming_count"]
        filtered_count = db.execute(
            "SELECT COUNT(DISTINCT c.id) AS client_count "
            "FROM clients c JOIN client_segments cs ON cs.client_id = c.id "
            "AND cs.segment = ? "
            "WHERE CASEFOLD(c.client_name || ' ' || COALESCE(c.phone, '')) LIKE ?",
            (EXCURSION_SEGMENT, search_pattern),
        ).fetchone()["client_count"]
    else:
        summary = db.execute(
            "SELECT COUNT(*) AS clients_count, "
            "COALESCE(SUM(os.order_count), 0) AS orders_count, "
            "COALESCE(SUM(os.order_total), 0) AS order_total, "
            "COALESCE(SUM(MAX(COALESCE(os.order_total, 0) - "
            " COALESCE(ps.paid_total, 0), 0)), 0) AS outstanding_total "
            "FROM clients c "
            "LEFT JOIN ("
            " SELECT client_id, COUNT(*) AS order_count, SUM(total) AS order_total "
            " FROM tuning_orders WHERE client_id IS NOT NULL GROUP BY client_id"
            ") os ON os.client_id = c.id "
            "LEFT JOIN ("
            " SELECT o.client_id, SUM(p.amount) AS paid_total "
            " FROM tuning_orders o JOIN tuning_payments p ON p.order_id = o.id "
            " WHERE o.client_id IS NOT NULL GROUP BY o.client_id"
            ") ps ON ps.client_id = c.id "
            f"WHERE {tuning_scope_sql}",
            (TUNING_SEGMENT,),
        ).fetchone()
        clients_count = summary["clients_count"]
        orders_count = summary["orders_count"]
        order_total = summary["order_total"]
        outstanding_total = summary["outstanding_total"]
        filtered_count = db.execute(
            "SELECT COUNT(*) AS client_count FROM clients c "
            f"WHERE {tuning_scope_sql} "
            "AND CASEFOLD(c.client_name || ' ' || COALESCE(c.phone, '')) LIKE ?",
            (TUNING_SEGMENT, search_pattern),
        ).fetchone()["client_count"]

    total_pages = max(
        1,
        (filtered_count + CLIENT_DIRECTORY_PAGE_SIZE - 1)
        // CLIENT_DIRECTORY_PAGE_SIZE,
    )
    page = min(requested_page, total_pages)
    offset = (page - 1) * CLIENT_DIRECTORY_PAGE_SIZE

    if section == EXCURSION_SEGMENT:
        client_rows = db.execute(
            "SELECT c.id, c.client_name, c.boat_model, c.phone, c.status, c.created_at, "
            "COUNT(DISTINCT si.id) AS trip_count, "
            "COUNT(DISTINCT CASE WHEN si.starts_at >= ? THEN si.id END) AS upcoming_count, "
            "MAX(CASE WHEN si.ends_at <= ? THEN si.starts_at END) AS last_trip_at, "
            "MIN(CASE WHEN si.starts_at >= ? THEN si.starts_at END) AS next_trip_at, "
            "GROUP_CONCAT(DISTINCT si.boat) AS boats "
            "FROM clients c JOIN client_segments cs ON cs.client_id = c.id "
            "AND cs.segment = ? "
            "LEFT JOIN schedule_participants sp ON sp.client_id = c.id "
            "LEFT JOIN schedule_items si ON si.id = sp.schedule_item_id "
            "AND si.deleted_at IS NULL "
            "WHERE CASEFOLD(c.client_name || ' ' || COALESCE(c.phone, '')) LIKE ? "
            "GROUP BY c.id "
            "ORDER BY COALESCE(MAX(si.starts_at), c.created_at) DESC, c.id DESC "
            "LIMIT ? OFFSET ?",
            (
                now,
                now,
                now,
                EXCURSION_SEGMENT,
                search_pattern,
                CLIENT_DIRECTORY_PAGE_SIZE,
                offset,
            ),
        ).fetchall()
        clients = [dict(row) for row in client_rows]
    else:
        client_rows = db.execute(
            "SELECT c.id, c.client_name, c.boat_model, c.phone, c.status, c.created_at, "
            "COALESCE(os.order_count, 0) AS order_count, "
            "COALESCE(os.order_total, 0) AS order_total, "
            "os.last_order_at, COALESCE(ps.paid_total, 0) AS paid_total, "
            "(SELECT CASE WHEN latest.equipment_type = 'motor' "
            "        THEN latest.motor_model ELSE latest.boat_model END "
            " FROM tuning_orders latest WHERE latest.client_id = c.id "
            " ORDER BY latest.order_date DESC, latest.id DESC LIMIT 1) "
            "AS latest_equipment_model, "
            "(SELECT latest.equipment_type FROM tuning_orders latest "
            " WHERE latest.client_id = c.id "
            " ORDER BY latest.order_date DESC, latest.id DESC LIMIT 1) "
            "AS latest_equipment_type "
            "FROM clients c "
            "LEFT JOIN ("
            " SELECT client_id, COUNT(*) AS order_count, SUM(total) AS order_total, "
            " MAX(order_date) AS last_order_at FROM tuning_orders "
            " WHERE client_id IS NOT NULL GROUP BY client_id"
            ") os ON os.client_id = c.id "
            "LEFT JOIN ("
            " SELECT o.client_id, SUM(p.amount) AS paid_total "
            " FROM tuning_orders o JOIN tuning_payments p ON p.order_id = o.id "
            " WHERE o.client_id IS NOT NULL GROUP BY o.client_id"
            ") ps ON ps.client_id = c.id "
            f"WHERE {tuning_scope_sql} "
            "AND CASEFOLD(c.client_name || ' ' || COALESCE(c.phone, '')) LIKE ? "
            "ORDER BY COALESCE(os.last_order_at, c.created_at) DESC, c.id DESC "
            "LIMIT ? OFFSET ?",
            (
                TUNING_SEGMENT,
                search_pattern,
                CLIENT_DIRECTORY_PAGE_SIZE,
                offset,
            ),
        ).fetchall()
        clients = []
        for row in client_rows:
            client = dict(row)
            client["outstanding"] = max(
                0.0, client["order_total"] - client["paid_total"]
            )
            clients.append(client)

    return render_template(
        "tuning_clients.html",
        clients=clients,
        clients_count=clients_count,
        orders_count=orders_count,
        order_total=order_total,
        outstanding_total=outstanding_total,
        trips_count=trips_count,
        upcoming_count=upcoming_count,
        tuning_clients_count=segment_counts.get(TUNING_SEGMENT, 0),
        excursion_clients_count=segment_counts.get(EXCURSION_SEGMENT, 0),
        client_section=section,
        client_import_notice=session.pop("client_import_notice", None),
        yclients_import_configured=yclients_configured(),
        client_statuses=CLIENT_STATUSES,
        active_page="clients",
        manager_view=manager_view,
        search_query=search_query,
        page=page,
        total_pages=total_pages,
        pagination_items=_client_pagination_items(page, total_pages),
        filtered_count=filtered_count,
        page_first=(offset + 1 if filtered_count else 0),
        page_last=min(offset + len(clients), filtered_count),
    )


@app.route("/admin/clients/<int:client_id>/status", methods=["POST"])
@excursion_manager_or_admin_required
def update_tuning_client_status(client_id):
    status = request.form.get("status", "").strip()
    allowed_statuses = {item["value"] for item in CLIENT_STATUSES}
    if status in allowed_statuses:
        db = get_db()
        if not _is_customer_manager(db) or db.execute(
            "SELECT 1 FROM client_segments WHERE client_id = ? AND segment = ?",
            (client_id, EXCURSION_SEGMENT),
        ).fetchone() is not None:
            db.execute("UPDATE clients SET status = ? WHERE id = ?", (status, client_id))
            db.commit()
    manager_view = _is_customer_manager()
    section = (
        EXCURSION_SEGMENT
        if manager_view
        else request.form.get("section", TUNING_SEGMENT)
    )
    if section not in (TUNING_SEGMENT, EXCURSION_SEGMENT):
        section = TUNING_SEGMENT
    redirect_args = {}
    if section == EXCURSION_SEGMENT:
        redirect_args["section"] = section
    page_value = request.form.get("page", "1")
    if page_value != "1":
        redirect_args["page"] = page_value
    search_query = " ".join(request.form.get("q", "").split())[:120]
    if search_query:
        redirect_args["q"] = search_query
    return redirect(url_for("tuning_clients", **redirect_args))


@app.route("/admin/clients/import-yclients", methods=["POST"])
@excursion_manager_or_admin_required
def import_yclients_client_directory():
    if not yclients_configured():
        session["client_import_notice"] = {
            "type": "error",
            "message": "Не настроены токены YCLIENTS в .env.",
        }
        return redirect(url_for("tuning_clients", section=EXCURSION_SEGMENT))
    try:
        stats = sync_yclients_clients(
            get_db(),
            dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            YCLIENTS_API_BASE,
            YCLIENTS_COMPANY_ID,
            YCLIENTS_PARTNER_TOKEN,
            YCLIENTS_USER_TOKEN,
        )
        session["client_import_notice"] = {
            "type": "success",
            "message": (
                f"YCLIENTS: получено {stats['received']}, создано "
                f"{stats['created']}, обновлено {stats['updated']}, "
                f"связано с существующими {stats['linked']}, "
                f"пропущено {stats['skipped']}."
            ),
        }
    except Exception as error:
        app.logger.exception("YCLIENTS client directory import failed")
        session["client_import_notice"] = {
            "type": "error",
            "message": f"Импорт клиентов YCLIENTS не выполнен: {error}",
        }
    return redirect(url_for("tuning_clients", section=EXCURSION_SEGMENT))


def _tuning_equipment_catalog_endpoint(equipment_type):
    return "tuning_motor_catalog" if equipment_type == "motor" else "tuning_boat_catalog"


def _tuning_equipment_profile_endpoint(equipment_type):
    return "tuning_motor_profile" if equipment_type == "motor" else "tuning_boat_profile"


def _render_tuning_equipment_catalog(equipment_type):
    db = get_db()
    _sync_tuning_boat_profiles(db)
    db.commit()
    model_column = "motor_model" if equipment_type == "motor" else "boat_model"

    order_rows = db.execute(
        "SELECT o.*, "
        "(SELECT GROUP_CONCAT(i.work_name, ' · ') FROM tuning_order_items i "
        " WHERE i.order_id = o.id AND i.status != 'removed') AS work_names "
        f"FROM tuning_orders o WHERE o.equipment_type = ? "
        f"AND TRIM(o.{model_column}) != '' "
        "ORDER BY o.order_date DESC, o.id DESC",
        (equipment_type,),
    ).fetchall()
    orders_by_model = {}
    for row in order_rows:
        orders_by_model.setdefault(
            _tuning_equipment_profile_key(equipment_type, row[model_column]), []
        ).append(row)

    profiles = []
    for row in db.execute(
        "SELECT * FROM tuning_boat_profiles WHERE equipment_type = ? "
        "ORDER BY model_name COLLATE NOCASE",
        (equipment_type,),
    ).fetchall():
        profile_orders = orders_by_model.get(row["model_key"], [])
        if not profile_orders:
            continue
        profile = dict(row)
        profile["photo_url"] = _tuning_boat_photo_url(profile)
        profile["order_count"] = len(profile_orders)
        profile["orders_total"] = sum(order["total"] for order in profile_orders)
        profile["last_order_at"] = profile_orders[0]["order_date"]
        profiles.append(profile)

    return render_template(
        "tuning_index.html",
        active_page="tuning",
        sub_page=f"{equipment_type}_catalog",
        catalog_view="list",
        page_title=("Каталог моторов" if equipment_type == "motor" else "Каталог лодок"),
        boat_profiles=profiles,
        catalog_order_count=sum(profile["order_count"] for profile in profiles),
        catalog_equipment_type=equipment_type,
    )


@app.route("/tuning/boats")
@admin_login_required
def tuning_boat_catalog():
    return _render_tuning_equipment_catalog("boat")


@app.route("/tuning/motors")
@admin_login_required
def tuning_motor_catalog():
    return _render_tuning_equipment_catalog("motor")


def _render_tuning_equipment_profile(profile_id, expected_type):
    db = get_db()
    profile_row = db.execute(
        "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if profile_row is None:
        return redirect(url_for(_tuning_equipment_catalog_endpoint(expected_type)))
    if profile_row["equipment_type"] != expected_type:
        return redirect(url_for(
            _tuning_equipment_profile_endpoint(profile_row["equipment_type"]),
            profile_id=profile_id,
        ))

    profile = dict(profile_row)
    profile["photo_url"] = _tuning_boat_photo_url(profile)
    profile["specification_items"] = _parse_boat_specifications(
        profile["specifications"]
    )
    model_column = "motor_model" if expected_type == "motor" else "boat_model"
    orders = []
    for row in db.execute(
        "SELECT o.*, "
        "(SELECT GROUP_CONCAT(i.work_name, ' · ') FROM tuning_order_items i "
        " WHERE i.order_id = o.id AND i.status != 'removed') AS work_names "
        "FROM tuning_orders o WHERE o.equipment_type = ? "
        f"AND TRIM(o.{model_column}) != '' "
        "ORDER BY o.order_date DESC, o.id DESC",
        (expected_type,),
    ).fetchall():
        if _tuning_equipment_profile_key(
            expected_type, row[model_column]
        ) == profile["model_key"]:
            orders.append(row)

    return render_template(
        "tuning_index.html",
        active_page="tuning",
        sub_page=f"{expected_type}_catalog",
        catalog_view="profile",
        page_title=profile["model_name"],
        boat_profile=profile,
        profile_orders=orders,
        profile_orders_total=sum(order["total"] for order in orders),
        profile_notice=session.pop("boat_profile_notice", None),
        profile_error=session.pop("boat_profile_error", None),
        profile_name_editor_open=session.pop(
            "boat_profile_name_editor_open", False
        ),
        profile_type_editor_open=session.pop(
            "boat_profile_type_editor_open", False
        ),
        catalog_equipment_type=expected_type,
    )


@app.route("/tuning/boats/<int:profile_id>")
@admin_login_required
def tuning_boat_profile(profile_id):
    return _render_tuning_equipment_profile(profile_id, "boat")


@app.route("/tuning/motors/<int:profile_id>")
@admin_login_required
def tuning_motor_profile(profile_id):
    return _render_tuning_equipment_profile(profile_id, "motor")


@app.route("/tuning/boats/<int:profile_id>/edit", methods=["POST"])
@app.route("/tuning/equipment/<int:profile_id>/edit", methods=["POST"])
@admin_login_required
def update_tuning_boat_profile(profile_id):
    db = get_db()
    profile = db.execute(
        "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if profile is None:
        return redirect(url_for("tuning_boat_catalog"))
    profile_endpoint = _tuning_equipment_profile_endpoint(profile["equipment_type"])

    raw_model_name = request.form.get("model_name")
    model_name = " ".join(
        (profile["model_name"] if raw_model_name is None else raw_model_name).split()
    )
    model_key = _tuning_equipment_profile_key(profile["equipment_type"], model_name)
    specifications = request.form.get("specifications", "").strip()
    source_url = request.form.get("specifications_source_url", "").strip()
    source_name = request.form.get("specifications_source_name", "").strip()
    if not model_name:
        session["boat_profile_error"] = "Укажите название модели."
        session["boat_profile_name_editor_open"] = True
        return redirect(url_for(profile_endpoint, profile_id=profile_id))
    if len(model_name) > TUNING_BOAT_MODEL_NAME_LIMIT:
        session["boat_profile_error"] = (
            "Название модели не должно превышать "
            f"{TUNING_BOAT_MODEL_NAME_LIMIT} символов."
        )
        session["boat_profile_name_editor_open"] = True
        return redirect(url_for(profile_endpoint, profile_id=profile_id))
    duplicate = db.execute(
        "SELECT id FROM tuning_boat_profiles WHERE model_key = ? AND id != ?",
        (model_key, profile_id),
    ).fetchone()
    if duplicate is not None:
        session["boat_profile_error"] = (
            "Модель с таким названием уже есть в каталоге."
        )
        session["boat_profile_name_editor_open"] = True
        return redirect(url_for(profile_endpoint, profile_id=profile_id))
    if len(specifications) > TUNING_BOAT_SPECIFICATIONS_LIMIT:
        session["boat_profile_error"] = (
            "Характеристики не должны превышать "
            f"{TUNING_BOAT_SPECIFICATIONS_LIMIT} символов."
        )
        return redirect(url_for(profile_endpoint, profile_id=profile_id))
    if len(source_url) > TUNING_BOAT_SOURCE_URL_LIMIT or (
        source_url and not source_url.startswith(("https://", "http://"))
    ):
        session["boat_profile_error"] = "Укажите корректную ссылку на источник."
        return redirect(url_for(profile_endpoint, profile_id=profile_id))
    if len(source_name) > TUNING_BOAT_SOURCE_NAME_LIMIT:
        session["boat_profile_error"] = (
            "Название источника не должно превышать "
            f"{TUNING_BOAT_SOURCE_NAME_LIMIT} символов."
        )
        return redirect(url_for(profile_endpoint, profile_id=profile_id))

    photo_filename = profile["photo_filename"]
    photo = request.files.get("photo")
    if photo and photo.filename:
        extension = os.path.splitext(photo.filename)[1].lower()
        if extension not in TUNING_BOAT_PHOTO_EXTENSIONS:
            session["boat_profile_error"] = (
                "Фотография должна быть в формате JPG, PNG или WebP."
            )
            return redirect(url_for(profile_endpoint, profile_id=profile_id))
        photos_dir = os.path.join(app.static_folder, "tuning_boats")
        os.makedirs(photos_dir, exist_ok=True)
        photo_filename = f"{profile_id}-{secrets.token_hex(6)}{extension}"
        photo.save(os.path.join(photos_dir, photo_filename))

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    renamed = model_key != profile["model_key"] or model_name != profile["model_name"]
    if renamed:
        _rename_tuning_boat_profile(db, profile, model_name, now)
    db.execute(
        "UPDATE tuning_boat_profiles "
        "SET specifications = ?, specifications_source_url = ?, "
        "specifications_source_name = ?, photo_filename = ?, updated_at = ? "
        "WHERE id = ?",
        (specifications, source_url, source_name, photo_filename, now, profile_id),
    )
    db.commit()
    session["boat_profile_notice"] = (
        "Название модели и связанные записи обновлены."
        if renamed
        else (
            "Профиль мотора обновлён."
            if profile["equipment_type"] == "motor"
            else "Профиль лодки обновлён."
        )
    )
    return redirect(url_for(profile_endpoint, profile_id=profile_id))


@app.route("/tuning/equipment/<int:profile_id>/type", methods=["POST"])
@admin_login_required
def update_tuning_equipment_profile_type(profile_id):
    db = get_db()
    profile = db.execute(
        "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id,)
    ).fetchone()
    if profile is None:
        return redirect(url_for("tuning_boat_catalog"))

    source_type = profile["equipment_type"]
    target_type = request.form.get("equipment_type", "").strip()
    source_endpoint = _tuning_equipment_profile_endpoint(source_type)
    if target_type not in ("boat", "motor") or target_type == source_type:
        return redirect(url_for(source_endpoint, profile_id=profile_id))

    if source_type == "boat":
        diagnostic_count = db.execute(
            "SELECT COUNT(*) FROM field_diagnostic_sheets "
            "WHERE boat_profile_id = ?",
            (profile_id,),
        ).fetchone()[0]
        if diagnostic_count:
            session["boat_profile_error"] = (
                "Карточку нельзя перенести в моторы, пока с ней связаны "
                "диагностические листы лодки."
            )
            session["boat_profile_type_editor_open"] = True
            return redirect(url_for(source_endpoint, profile_id=profile_id))

    target_key = _tuning_equipment_profile_key(target_type, profile["model_name"])
    target_profile = db.execute(
        "SELECT * FROM tuning_boat_profiles "
        "WHERE model_key = ? AND equipment_type = ? AND id != ?",
        (target_key, target_type, profile_id),
    ).fetchone()
    canonical_name = (
        target_profile["model_name"] if target_profile is not None
        else profile["model_name"]
    )
    source_model_column = "motor_model" if source_type == "motor" else "boat_model"
    order_rows = db.execute(
        f"SELECT id, client_id, {source_model_column} AS model_name "
        "FROM tuning_orders WHERE equipment_type = ?",
        (source_type,),
    ).fetchall()
    order_ids = [
        row["id"] for row in order_rows
        if _tuning_equipment_profile_key(source_type, row["model_name"])
        == profile["model_key"]
    ]
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    if target_type == "motor":
        db.executemany(
            "UPDATE tuning_orders SET equipment_type = 'motor', boat_model = '', "
            "boat_registration_number = '', motor_model = ?, updated_at = ? "
            "WHERE id = ?",
            [(canonical_name, now, order_id) for order_id in order_ids],
        )
        for client in db.execute("SELECT id, boat_model FROM clients").fetchall():
            if _normalize_tuning_boat_model(client["boat_model"]) == profile["model_key"]:
                db.execute(
                    "UPDATE clients SET boat_model = '' WHERE id = ?", (client["id"],)
                )
    else:
        db.executemany(
            "UPDATE tuning_orders SET equipment_type = 'boat', boat_model = ?, "
            "motor_model = '', motor_serial_number = '', updated_at = ? WHERE id = ?",
            [(canonical_name, now, order_id) for order_id in order_ids],
        )
        linked_client_ids = {
            row["client_id"] for row in order_rows
            if row["id"] in order_ids and row["client_id"] is not None
        }
        if linked_client_ids:
            db.executemany(
                "UPDATE clients SET boat_model = ? WHERE id = ?",
                [(canonical_name, client_id) for client_id in linked_client_ids],
            )

    if target_profile is not None:
        db.execute(
            "UPDATE tuning_boat_profiles SET photo_filename = ?, specifications = ?, "
            "specifications_source_url = ?, specifications_source_name = ?, "
            "updated_at = ? WHERE id = ?",
            (
                target_profile["photo_filename"] or profile["photo_filename"],
                target_profile["specifications"] or profile["specifications"],
                target_profile["specifications_source_url"]
                or profile["specifications_source_url"],
                target_profile["specifications_source_name"]
                or profile["specifications_source_name"],
                now,
                target_profile["id"],
            ),
        )
        db.execute("DELETE FROM tuning_boat_profiles WHERE id = ?", (profile_id,))
        destination_id = target_profile["id"]
    else:
        db.execute(
            "UPDATE tuning_boat_profiles SET equipment_type = ?, model_key = ?, "
            "updated_at = ? WHERE id = ?",
            (target_type, target_key, now, profile_id),
        )
        destination_id = profile_id

    if target_type == "boat":
        _seed_tuning_boat_profile_specifications(db)
    db.commit()
    session["boat_profile_notice"] = (
        "Карточка перенесена в каталог моторов."
        if target_type == "motor"
        else "Карточка перенесена в каталог лодок."
    )
    return redirect(url_for(
        _tuning_equipment_profile_endpoint(target_type),
        profile_id=destination_id,
    ))


app.register_blueprint(
    create_field_diagnostics_blueprint(
        get_db=get_db,
        admin_login_required=admin_login_required,
        normalize_boat_model=_normalize_tuning_boat_model,
        boat_model_choices=_tuning_boat_model_choices,
        mark_tuning_client=lambda db, client_id, created_at: ensure_client_segment(
            db, client_id, TUNING_SEGMENT, created_at
        ),
    )
)


@app.route("/tuning/diagnostics/hull")
@admin_login_required
def tuning_diagnostics():
    db = get_db()
    sheets = db.execute(
        "SELECT * FROM hull_diagnostic_sheets ORDER BY id DESC"
    ).fetchall()
    return render_template(
        "tuning_diagnostics.html", active_page="tuning", sub_page="diagnostics",
        diag_page="hull", sheets=sheets,
    )


@app.route("/tuning/diagnostics/hull/add", methods=["POST"])
@admin_login_required
def add_hull_diagnostic_sheet():
    boat_name = request.form.get("boat_name", "").strip()
    if boat_name:
        db = get_db()
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO hull_diagnostic_sheets (boat_name, created_at) VALUES (?, ?)",
            (boat_name, now),
        )
        db.commit()
    return redirect(url_for("tuning_diagnostics"))


HULL_VIEWS = ("bottom", "left", "right", "top")
HULL_VIEW_LABELS = {
    "bottom": "Днище", "top": "Палуба", "left": "Левый борт", "right": "Правый борт",
}
app.jinja_env.filters["hull_view_label"] = lambda v: HULL_VIEW_LABELS.get(v, v)


@app.route("/tuning/diagnostics/hull/<int:sheet_id>")
@admin_login_required
def hull_diagnostic_sheet(sheet_id):
    db = get_db()
    sheet = db.execute(
        "SELECT * FROM hull_diagnostic_sheets WHERE id = ?", (sheet_id,)
    ).fetchone()
    if sheet is None:
        return redirect(url_for("tuning_diagnostics"))

    defects = db.execute(
        "SELECT * FROM hull_diagnostic_defects WHERE sheet_id = ? ORDER BY id", (sheet_id,)
    ).fetchall()
    defects_by_view = {v: [] for v in HULL_VIEWS}
    for d in defects:
        if d["view"] in defects_by_view:
            defects_by_view[d["view"]].append(d)

    return render_template(
        "hull_diagnostic_sheet.html", sheet=sheet, defects_by_view=defects_by_view,
        defects=defects, active_page="tuning", sub_page="diagnostics", diag_page="hull",
    )


@app.route("/tuning/diagnostics/hull/<int:sheet_id>/defect/add", methods=["POST"])
@admin_login_required
def add_hull_diagnostic_defect(sheet_id):
    db = get_db()
    sheet = db.execute(
        "SELECT id FROM hull_diagnostic_sheets WHERE id = ?", (sheet_id,)
    ).fetchone()
    if sheet is None:
        return jsonify({"error": "Лист не найден."}), 404

    view = request.form.get("view", "").strip()
    if view not in HULL_VIEWS:
        return jsonify({"error": "Неизвестный вид схемы."}), 400

    try:
        x_pct = float(request.form.get("x_pct", ""))
        y_pct = float(request.form.get("y_pct", ""))
    except ValueError:
        return jsonify({"error": "Некорректные координаты."}), 400
    x_pct = min(100.0, max(0.0, x_pct))
    y_pct = min(100.0, max(0.0, y_pct))

    defect_type = request.form.get("defect_type", "").strip()
    defect_size = request.form.get("defect_size", "").strip()
    if not defect_type:
        return jsonify({"error": "Укажите тип дефекта."}), 400
    if not defect_size:
        return jsonify({"error": "Укажите размер дефекта."}), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = db.execute(
        "INSERT INTO hull_diagnostic_defects "
        "(sheet_id, view, x_pct, y_pct, defect_type, defect_size, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sheet_id, view, x_pct, y_pct, defect_type, defect_size, now),
    )
    db.commit()
    return jsonify({
        "id": cur.lastrowid, "view": view, "x_pct": x_pct, "y_pct": y_pct,
        "defect_type": defect_type, "defect_size": defect_size,
    })


@app.route("/tuning/diagnostics/hull/<int:sheet_id>/defect/<int:defect_id>/delete", methods=["POST"])
@admin_login_required
def delete_hull_diagnostic_defect(sheet_id, defect_id):
    db = get_db()
    db.execute(
        "DELETE FROM hull_diagnostic_defects WHERE id = ? AND sheet_id = ?",
        (defect_id, sheet_id),
    )
    db.commit()
    return redirect(url_for("hull_diagnostic_sheet", sheet_id=sheet_id))


@app.route("/tuning/edit/<int:order_id>/hull-sheet/create", methods=["POST"])
@admin_login_required
def create_and_link_hull_sheet(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))
    if order["equipment_type"] != "boat":
        return redirect(url_for("edit_tuning_order", order_id=order_id))
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = db.execute(
        "INSERT INTO hull_diagnostic_sheets (boat_name, created_at, tuning_order_id) VALUES (?, ?, ?)",
        (order["boat_model"], now, order_id),
    )
    db.commit()
    return redirect(url_for("hull_diagnostic_sheet", sheet_id=cur.lastrowid))


@app.route("/tuning/edit/<int:order_id>/hull-sheet/link", methods=["POST"])
@admin_login_required
def link_hull_sheet(order_id):
    db = get_db()
    order = db.execute(
        "SELECT equipment_type FROM tuning_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))
    if order["equipment_type"] != "boat":
        return redirect(url_for("edit_tuning_order", order_id=order_id))
    sheet_id = request.form.get("sheet_id", "").strip()
    if sheet_id.isdigit():
        db.execute(
            "UPDATE hull_diagnostic_sheets SET tuning_order_id = ? WHERE id = ? AND tuning_order_id IS NULL",
            (order_id, int(sheet_id)),
        )
        db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/edit/<int:order_id>/hull-sheet/<int:sheet_id>/unlink", methods=["POST"])
@admin_login_required
def unlink_hull_sheet(order_id, sheet_id):
    db = get_db()
    db.execute(
        "UPDATE hull_diagnostic_sheets SET tuning_order_id = NULL WHERE id = ? AND tuning_order_id = ?",
        (sheet_id, order_id),
    )
    db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/add", methods=["GET", "POST"])
@admin_login_required
def add_tuning_order():
    if request.method == "GET":
        db = get_db()
        _sync_tuning_boat_profiles(db)
        db.commit()
        return render_template(
            "tuning_form.html", edit_order=None, errors=None, form_values=None,
            items_prefill=None, sale_channels=SALE_CHANNELS, active_page="tuning", sub_page="orders",
            today=dt.date.today().isoformat(),
            boat_model_choices=_tuning_boat_model_choices(db),
            tuning_client_choices=_tuning_client_choices(db),
        )

    db = get_db()
    errors, data = _process_tuning_form(request.form)
    if errors:
        return render_template(
            "tuning_form.html", edit_order=None, errors=errors, form_values=request.form,
            items_prefill=None, sale_channels=SALE_CHANNELS, active_page="tuning", sub_page="orders",
            today=dt.date.today().isoformat(),
            boat_model_choices=_tuning_boat_model_choices(db),
            tuning_client_choices=_tuning_client_choices(db),
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    client_id = _get_or_create_client(
        db, data["phone"], data["client_name"], data["boat_model"],
        data["client_id"],
    )
    cur = db.execute(
        "INSERT INTO tuning_orders (client_id, client_name, equipment_type, boat_model, "
        "boat_registration_number, motor_model, motor_serial_number, sale_channel, phone, "
        "discount_pct, discount_type, discount_value, subtotal, total, status, order_date, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (client_id, data["client_name"], data["equipment_type"], data["boat_model"],
         data["boat_registration_number"], data["motor_model"],
         data["motor_serial_number"], data["sale_channel"], data["phone"],
         data["discount_pct"], data["discount_type"], data["discount_value"],
         data["subtotal"], data["total"], DEFAULT_ORDER_STATUS,
         data["order_date"], now, now),
    )
    order_id = cur.lastrowid
    for item in data["items"]:
        db.execute(
            "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, item["work_name"], item["cost_price"], item["multiplier"], item["price"],
             DEFAULT_WORK_STATUS),
        )
    db.execute(
        "INSERT INTO projects (name, tuning_order_id, created_at) VALUES (?, ?, ?)",
        (f"Заказ №{order_id}", order_id, now),
    )
    _sync_tuning_boat_profiles(db)
    db.commit()
    return redirect(url_for("tuning_index"))


# Fixed JSON contract for the standalone tuning.bodrbo.ru site. Keeping
# these limits here makes the public boundary explicit and prevents an
# accidental multi-megabyte lead from being copied into orders/notes.
_TUNING_LEAD_TEXT_LIMITS = {
    "request_id": 128,
    "name": 200,
    "phone": 64,
    "boat_model": 200,
    "message": 5000,
    "source_url": 2048,
    "submitted_at": 100,
}
_TUNING_LEAD_REQUIRED_FIELDS = ("request_id", "name", "phone")


def _tuning_lead_json_error(status, error, message):
    return jsonify(ok=False, error=error, message=message), status


def _validate_tuning_lead_payload(payload):
    """Validate and normalize the public tuning-site lead contract."""
    if not isinstance(payload, dict):
        return None, "Тело запроса должно быть JSON-объектом."

    data = {}
    for field, limit in _TUNING_LEAD_TEXT_LIMITS.items():
        value = payload.get(field)
        if value is None and field not in _TUNING_LEAD_REQUIRED_FIELDS:
            value = ""
        if not isinstance(value, str):
            return None, f"Поле «{field}» должно быть строкой."
        value = value.strip()
        if field in _TUNING_LEAD_REQUIRED_FIELDS and not value:
            return None, f"Поле «{field}» обязательно."
        if len(value) > limit:
            return None, f"Поле «{field}» длиннее допустимых {limit} символов."
        data[field] = value
    return data, None


def _tuning_site_token_is_valid(authorization):
    scheme, separator, supplied = (authorization or "").partition(" ")
    token = supplied.strip() if separator and scheme.lower() == "bearer" else ""
    # Compare bytes so arbitrary non-ASCII input cannot make compare_digest
    # raise; the comparison itself remains constant-time for the supplied
    # byte strings.
    return secrets.compare_digest(
        token.encode("utf-8"), TUNING_SITE_WEBHOOK_SECRET.encode("utf-8")
    )


@app.route("/api/integrations/tuning-leads", methods=["POST"])
def tuning_site_lead_webhook():
    """Create one tuning-center lead from tuning.bodrbo.ru JSON."""
    if not TUNING_SITE_WEBHOOK_SECRET:
        return _tuning_lead_json_error(
            503,
            "integration_not_configured",
            "Интеграция с сайтом тюнинга не настроена.",
        )
    if not _tuning_site_token_is_valid(request.headers.get("Authorization", "")):
        response, status = _tuning_lead_json_error(
            401, "unauthorized", "Неверный токен авторизации."
        )
        response.headers["WWW-Authenticate"] = "Bearer"
        return response, status

    payload = request.get_json(silent=True) if request.is_json else None
    data, validation_error = _validate_tuning_lead_payload(payload)
    if validation_error:
        return _tuning_lead_json_error(
            400, "invalid_payload", validation_error
        )

    db = get_db()
    source_ref = f"tuning_site:{data['request_id']}"
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    order_date = _external_order_date(data["submitted_at"], now[:10])
    try:
        # Serializing this short check/create transaction makes request_id
        # idempotent even when the site retries concurrently.
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref = ?", (source_ref,)
        ).fetchone()
        if existing is not None:
            db.commit()
            return jsonify(
                ok=True, duplicate=True, order_id=existing["id"]
            ), 200

        client_id = _get_or_create_client(
            db, data["phone"], data["name"], data["boat_model"]
        )
        cursor = db.execute(
            "INSERT INTO tuning_orders "
            "(client_id, client_name, boat_model, sale_channel, phone, "
            "discount_pct, discount_type, discount_value, subtotal, total, "
            "status, source, source_ref, order_date, created_at, updated_at) "
            "VALUES (?, ?, ?, 'direct', ?, 0, 'percent', 0, 0, 0, "
            "'new_request', 'tuning_site', ?, ?, ?, ?)",
            (
                client_id,
                data["name"],
                data["boat_model"],
                data["phone"],
                source_ref,
                order_date,
                now,
                now,
            ),
        )
        order_id = cursor.lastrowid
        db.execute(
            "INSERT INTO projects (name, tuning_order_id, created_at) "
            "VALUES (?, ?, ?)",
            (f"Заказ №{order_id}", order_id, now),
        )

        note_lines = ["Заявка с tuning.bodrbo.ru."]
        if data["message"]:
            note_lines.append(f"Сообщение: {data['message']}")
        if data["source_url"]:
            note_lines.append(f"Страница отправки: {data['source_url']}")
        note_lines.append(
            "Время отправки: " + (data["submitted_at"] or now)
        )
        db.execute(
            "INSERT INTO tuning_order_notes "
            "(order_id, author_admin_id, text, created_at) "
            "VALUES (?, NULL, ?, ?)",
            (order_id, "\n".join(note_lines), now),
        )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Failed to create tuning-site lead")
        return _tuning_lead_json_error(
            503,
            "service_unavailable",
            "Не удалось сохранить заявку. Повторите запрос позже.",
        )

    return jsonify(ok=True, order_id=order_id), 201


# Field names in a Tilda webhook payload are whatever the site's form
# editor called them — there's no fixed schema — so this matches common
# Russian/English variants case-insensitively rather than assuming one
# exact name. Add more here if the real site form uses something else;
# anything not recognized still isn't lost (see _extract_tilda_lead_fields).
_TILDA_NAME_KEYS = {"name", "имя", "фио", "ваше имя", "имя и фамилия"}
_TILDA_PHONE_KEYS = {"phone", "телефон", "тел", "тел.", "номер телефона", "ваш телефон"}
_TILDA_SYSTEM_KEYS = {"tranid", "formid", "formname", "cookies", "test"}


def _extract_tilda_lead_fields(form):
    """Best-effort pull of name/phone out of a Tilda webhook payload. Every
    other field received (minus Tilda's own bookkeeping fields) is kept as
    raw_lines so nothing is lost even when a field isn't recognized — the
    admin sees the full submission in the order's first note and can fix
    up client_name/phone by hand if the guess is wrong."""
    name = phone = None
    raw_lines = []
    for key, value in form.items():
        value = (value or "").strip()
        if not value:
            continue
        key_lower = key.strip().lower()
        if key_lower in _TILDA_SYSTEM_KEYS:
            continue
        if name is None and key_lower in _TILDA_NAME_KEYS:
            name = value
        elif phone is None and key_lower in _TILDA_PHONE_KEYS:
            phone = value
        else:
            raw_lines.append(f"{key}: {value}")
    return name, phone, raw_lines


def _log_tilda_webhook(db, token_ok, result):
    # Logged for EVERY hit, including auth failures — the only way to
    # answer "is Tilda even calling us, and with what?" without server
    # console access. See /internal/tilda-webhook-log below.
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload_json = json.dumps(dict(request.form), ensure_ascii=False)
    db.execute(
        "INSERT INTO tilda_webhook_log (received_at, token_ok, payload_json, result) "
        "VALUES (?, ?, ?, ?)",
        (now, 1 if token_ok else 0, payload_json, result),
    )
    db.commit()


@app.route("/webhooks/tilda", methods=["POST"])
def tilda_webhook():
    """Receives a lead from the site's feedback form (Tilda → Site
    Settings → Forms → Webhook, configured with this URL plus
    ?token=TILDA_WEBHOOK_SECRET). Tilda posts form-encoded data and expects
    a 200 OK within 5 seconds, retrying up to twice (1 minute apart) if it
    doesn't get one — so a repeat delivery of the same submission (same
    tranid) is recognized and skipped instead of creating a duplicate
    order. Creates the order with status='new_request' ("Новая заявка")
    so it shows up distinctly in the orders list for an admin to pick up
    and fill in properly (boat model, pricing, etc. aren't in a feedback
    form — just name/phone/whatever else the form asks)."""
    db = get_db()
    token_ok = bool(TILDA_WEBHOOK_SECRET) and request.args.get("token") == TILDA_WEBHOOK_SECRET
    if not token_ok:
        _log_tilda_webhook(db, False, "forbidden — missing/wrong token")
        return "forbidden", 403

    # Tilda's own connectivity check when the webhook URL is saved in Site
    # Settings — nothing to create, just confirm we're reachable.
    if request.form.get("test") == "test":
        _log_tilda_webhook(db, True, "connectivity test (test=test)")
        return "ok", 200

    tranid = request.form.get("tranid", "").strip()
    if tranid:
        existing = db.execute(
            "SELECT id FROM tuning_orders WHERE source_ref = ?", (tranid,)
        ).fetchone()
        if existing is not None:
            _log_tilda_webhook(db, True, f"duplicate of order #{existing['id']}")
            return "ok (duplicate)", 200

    name, phone, raw_lines = _extract_tilda_lead_fields(request.form)
    client_name = name or "Заявка с сайта"
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    # A client cabinet can now be created from a name alone; blank phone
    # numbers are deliberately not used as an identity key.
    client_id = _get_or_create_client(db, phone or "", client_name, "") if client_name else None
    cur = db.execute(
        "INSERT INTO tuning_orders (client_id, client_name, boat_model, sale_channel, phone, "
        "discount_pct, discount_type, discount_value, subtotal, total, status, source, source_ref, "
        "order_date, created_at, updated_at) "
        "VALUES (?, ?, ?, 'direct', ?, 0, 'percent', 0, 0, 0, 'new_request', 'tilda', ?, ?, ?, ?)",
        (client_id, client_name, "", phone or "", tranid or None, now[:10], now, now),
    )
    order_id = cur.lastrowid
    db.execute(
        "INSERT INTO projects (name, tuning_order_id, created_at) VALUES (?, ?, ?)",
        (f"Заказ №{order_id}", order_id, now),
    )
    note_text = "Заявка с сайта (форма обратной связи, Тильда)."
    if raw_lines:
        note_text += "\n" + "\n".join(raw_lines)
    db.execute(
        "INSERT INTO tuning_order_notes (order_id, author_admin_id, text, created_at) VALUES (?, NULL, ?, ?)",
        (order_id, note_text, now),
    )
    db.commit()
    _log_tilda_webhook(db, True, f"created order #{order_id}")
    return "ok", 200


@app.route("/internal/tilda-webhook-log")
def tilda_webhook_log():
    """Visit this URL (with CRON_SECRET as token) to see the last 20 hits
    on /webhooks/tilda — including auth failures — to check whether Tilda
    is actually calling us at all, with what token, and what payload, when
    a lead doesn't show up as an order."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    db = get_db()
    rows = db.execute(
        "SELECT * FROM tilda_webhook_log ORDER BY id DESC LIMIT 20"
    ).fetchall()
    if not rows:
        return "No hits recorded yet on /webhooks/tilda.", 200
    lines = [
        f"{r['received_at']} | token_ok={'yes' if r['token_ok'] else 'NO'} | {r['result']}\n"
        f"  payload: {r['payload_json']}"
        for r in rows
    ]
    return "\n\n".join(lines), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/tuning/edit/<int:order_id>", methods=["GET", "POST"])
@admin_login_required
def edit_tuning_order(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))
    _sync_tuning_boat_profiles(db)
    db.commit()
    equipment_profile_id = _tuning_equipment_profile_id(
        db,
        order["equipment_type"],
        order["motor_model"]
        if order["equipment_type"] == "motor"
        else order["boat_model"],
    )
    boat_profile_id = (
        equipment_profile_id if order["equipment_type"] == "boat" else None
    )
    motor_profile_id = (
        equipment_profile_id if order["equipment_type"] == "motor" else None
    )

    if request.method == "GET":
        items = []
        for row in db.execute(
            "SELECT * FROM tuning_order_items WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall():
            item = dict(row)
            assignment_row = db.execute(
                "SELECT * FROM tuning_item_assignments WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                (item["id"],),
            ).fetchone()
            assignment = dict(assignment_row) if assignment_row else None
            item["assignment"] = assignment
            item["can_assign"] = item["status"] != "removed" and (
                assignment is None
                or assignment["assignment_status"] == "rejected"
                or (assignment["assignment_status"] == "accepted" and item["status"] == "done")
            )
            items.append(item)
        assignable_employees = _employees_with_any_position(db, TUNING_ASSIGNABLE_POSITIONS)
        goods = db.execute(
            "SELECT tuning_order_products.*, supply_warehouses.name AS writeoff_warehouse_name "
            "FROM tuning_order_products "
            "LEFT JOIN supply_writeoffs ON supply_writeoffs.tuning_order_product_id = tuning_order_products.id "
            "LEFT JOIN supply_warehouses ON supply_warehouses.id = supply_writeoffs.warehouse_id "
            "WHERE tuning_order_products.order_id = ? ORDER BY tuning_order_products.id",
            (order_id,),
        ).fetchall()
        goods_subtotal = sum(g["quantity"] * g["unit_price"] for g in goods)
        catalog_products = db.execute("SELECT * FROM supply_products ORDER BY name").fetchall()
        payments, paid_amount, remaining = _order_payment_totals(db, order_id, order["total"])
        yookassa_payments = db.execute(
            "SELECT * FROM tuning_yookassa_payments WHERE order_id = ? ORDER BY id DESC", (order_id,)
        ).fetchall()
        form_values = {
            "client_id": order["client_id"] or "",
            "client_name": order["client_name"],
            "equipment_type": order["equipment_type"],
            "boat_model": order["boat_model"],
            "boat_registration_number": order["boat_registration_number"],
            "motor_model": order["motor_model"],
            "motor_serial_number": order["motor_serial_number"],
            "sale_channel": order["sale_channel"], "phone": order["phone"],
            "order_date": order["order_date"],
            "discount_type": order["discount_type"], "discount_value": order["discount_value"],
        }
        hull_sheets = db.execute(
            "SELECT * FROM hull_diagnostic_sheets WHERE tuning_order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
        available_hull_sheets = db.execute(
            "SELECT * FROM hull_diagnostic_sheets WHERE tuning_order_id IS NULL ORDER BY boat_name"
        ).fetchall()
        work_photos_by_item = {item["id"]: get_work_item_photos(db, item["id"]) for item in items}
        notes = _order_notes(db, order_id)
        reminder_recipients = _note_reminder_recipients(db)
        return render_template(
            "tuning_form.html", edit_order=order, errors=None, form_values=form_values,
            items_prefill=items, sale_channels=SALE_CHANNELS, active_page="tuning", sub_page="orders",
            today=dt.date.today().isoformat(),
            payments=payments, paid_amount=paid_amount, remaining=remaining,
            order_statuses=ORDER_STATUSES, work_statuses=WORK_STATUSES,
            yookassa_payments=yookassa_payments, yookassa_configured=yookassa_configured(),
            yookassa_error=session.pop("yookassa_error", None),
            hull_sheets=hull_sheets, available_hull_sheets=available_hull_sheets,
            work_photos_by_item=work_photos_by_item,
            assignable_employees=assignable_employees,
            goods=goods, goods_subtotal=goods_subtotal, catalog_products=catalog_products,
            cost_units=SUPPLY_COST_UNITS,
            notes=notes, reminder_recipients=reminder_recipients,
            boat_profile_id=boat_profile_id,
            motor_profile_id=motor_profile_id,
            boat_model_choices=_tuning_boat_model_choices(db),
            tuning_client_choices=_tuning_client_choices(db),
            modulkassa_configured=_modulkassa_configured(),
        )

    errors, data = _process_tuning_form(
        request.form, default_order_date=order["order_date"]
    )
    if errors:
        payments, paid_amount, remaining = _order_payment_totals(db, order_id, order["total"])
        yookassa_payments = db.execute(
            "SELECT * FROM tuning_yookassa_payments WHERE order_id = ? ORDER BY id DESC", (order_id,)
        ).fetchall()
        hull_sheets = db.execute(
            "SELECT * FROM hull_diagnostic_sheets WHERE tuning_order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
        available_hull_sheets = db.execute(
            "SELECT * FROM hull_diagnostic_sheets WHERE tuning_order_id IS NULL ORDER BY boat_name"
        ).fetchall()
        return render_template(
            "tuning_form.html", edit_order=order, errors=errors, form_values=request.form,
            items_prefill=None, sale_channels=SALE_CHANNELS, active_page="tuning", sub_page="orders",
            today=dt.date.today().isoformat(),
            payments=payments, paid_amount=paid_amount, remaining=remaining,
            order_statuses=ORDER_STATUSES, work_statuses=WORK_STATUSES,
            yookassa_payments=yookassa_payments, yookassa_configured=yookassa_configured(),
            yookassa_error=None,
            hull_sheets=hull_sheets, available_hull_sheets=available_hull_sheets,
            boat_profile_id=boat_profile_id,
            motor_profile_id=motor_profile_id,
            boat_model_choices=_tuning_boat_model_choices(db),
            tuning_client_choices=_tuning_client_choices(db),
            modulkassa_configured=_modulkassa_configured(),
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    client_id = _get_or_create_client(
        db, data["phone"], data["client_name"], data["boat_model"],
        data["client_id"],
    )
    db.execute(
        "UPDATE tuning_orders SET client_id=?, client_name=?, equipment_type=?, boat_model=?, "
        "boat_registration_number=?, motor_model=?, motor_serial_number=?, sale_channel=?, phone=?, "
        "discount_pct=?, discount_type=?, discount_value=?, subtotal=?, total=?, order_date=?, updated_at=? WHERE id=?",
        (client_id, data["client_name"], data["equipment_type"], data["boat_model"],
         data["boat_registration_number"], data["motor_model"],
         data["motor_serial_number"], data["sale_channel"], data["phone"],
         data["discount_pct"], data["discount_type"], data["discount_value"],
         data["subtotal"], data["total"], data["order_date"], now, order_id),
    )
    # Update surviving rows in place instead of delete-all + reinsert-all —
    # the form resubmits every row on every save (even ones untouched
    # here), and a blanket delete+recreate used to hand every row a brand
    # new id on every single save, silently orphaning anything keyed on
    # tuning_order_items.id (task assignments, photos, and now the
    # per-work-item transaction links below) — not just resetting status,
    # which is why that used to be tracked separately by position.
    # Each row's own item_id[] (see work_row() macro) says whether it's an
    # existing row (UPDATE, id preserved) or a new one (INSERT); anything
    # that existed before but wasn't resubmitted was removed via the "✕"
    # button in the form, so its assignment/photos are cleaned up too.
    existing_ids = {
        r["id"] for r in db.execute(
            "SELECT id FROM tuning_order_items WHERE order_id = ?", (order_id,)
        ).fetchall()
    }
    submitted_ids = {item["item_id"] for item in data["items"] if item["item_id"] in existing_ids}
    for removed_id in existing_ids - submitted_ids:
        db.execute("DELETE FROM tuning_item_assignments WHERE item_id = ?", (removed_id,))
        db.execute("DELETE FROM work_item_photos WHERE item_id = ?", (removed_id,))
        db.execute("DELETE FROM tuning_order_items WHERE id = ?", (removed_id,))
    for item in data["items"]:
        if item["item_id"] in existing_ids:
            db.execute(
                "UPDATE tuning_order_items SET work_name=?, cost_price=?, multiplier=?, price=? WHERE id=?",
                (item["work_name"], item["cost_price"], item["multiplier"], item["price"], item["item_id"]),
            )
        else:
            db.execute(
                "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, item["work_name"], item["cost_price"], item["multiplier"], item["price"],
                 DEFAULT_WORK_STATUS),
            )
    _sync_tuning_boat_profiles(db)
    db.commit()
    # The UPDATE above wrote subtotal/total from work items alone (all
    # _process_tuning_form knows about) — fold in any goods added via the
    # separate "Товары" mini-form now that the new work rows are saved.
    _recompute_order_totals(db, order_id)
    return redirect(url_for("tuning_index"))


@app.route("/tuning/delete/<int:order_id>", methods=["POST"])
@admin_login_required
def delete_tuning_order(order_id):
    db = get_db()
    db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM tuning_orders WHERE id = ?", (order_id,))
    db.commit()
    return redirect(url_for("tuning_index"))


@app.route("/tuning/<int:order_id>/status", methods=["POST"])
@admin_login_required
def set_tuning_order_status(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))
    status = request.form.get("status", "").strip()
    if status in [s["value"] for s in ORDER_STATUSES]:
        db.execute("UPDATE tuning_orders SET status = ? WHERE id = ?", (status, order_id))
        db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/item/<int:item_id>/status", methods=["POST"])
@admin_login_required
def set_tuning_item_status(order_id, item_id):
    db = get_db()
    status = request.form.get("status", "").strip()
    if status in [s["value"] for s in WORK_STATUSES]:
        db.execute(
            "UPDATE tuning_order_items SET status = ? WHERE id = ? AND order_id = ?",
            (status, item_id, order_id),
        )
        db.commit()
        # Moving a work item to/from "Задача снята" changes whether its
        # price counts toward the order — keep subtotal/total in sync.
        _recompute_order_totals(db, order_id)
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/item/<int:item_id>/assign", methods=["POST"])
@admin_login_required
def assign_tuning_item(order_id, item_id):
    db = get_db()
    item = db.execute(
        "SELECT * FROM tuning_order_items WHERE id = ? AND order_id = ?", (item_id, order_id)
    ).fetchone()
    if item is None:
        return redirect(url_for("edit_tuning_order", order_id=order_id))

    employee_name = request.form.get("employee_name", "").strip()
    rate_raw = request.form.get("rate", "").strip().replace(",", ".")
    hours_raw = request.form.get("norm_hours", "").strip().replace(",", ".")
    comment = request.form.get("comment", "").strip()

    valid_employees = _employees_with_any_position(db, TUNING_ASSIGNABLE_POSITIONS)
    rate = hours = None
    try:
        rate = float(rate_raw)
    except ValueError:
        pass
    try:
        hours = float(hours_raw)
    except ValueError:
        pass

    if (
        employee_name in valid_employees
        and rate is not None
        and rate > 0
        and hours is not None
        and hours > 0
        and len(comment) <= TASK_ASSIGNMENT_COMMENT_MAX_LENGTH
    ):
        cur = db.execute(
            "INSERT INTO tuning_item_assignments "
            "(item_id, employee_name, rate, norm_hours, comment, assignment_status, assigned_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (
                item_id,
                employee_name,
                rate,
                hours,
                comment,
                dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
        )
        db.commit()
        _notify_task_assignment(db, ASSIGNMENT_TUNING, cur.lastrowid)
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/item/<int:item_id>/photo", methods=["POST"])
@admin_login_required
def upload_tuning_item_photo(order_id, item_id):
    db = get_db()
    item = db.execute(
        "SELECT id FROM tuning_order_items WHERE id = ? AND order_id = ?", (item_id, order_id)
    ).fetchone()
    file = request.files.get("photo")
    comment = request.form.get("comment", "").strip()
    if item is not None and file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext in WORK_PHOTO_EXTENSIONS:
            photos_dir = os.path.join(app.static_folder, "work_photos")
            os.makedirs(photos_dir, exist_ok=True)
            filename = f"{item_id}-{secrets.token_hex(6)}{ext}"
            file.save(os.path.join(photos_dir, filename))
            db.execute(
                "INSERT INTO work_item_photos (item_id, filename, comment, created_at) "
                "VALUES (?, ?, ?, ?)",
                (item_id, filename, comment or None, dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/pay", methods=["POST"])
@admin_login_required
def add_tuning_payment(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))

    amount_raw = request.form.get("amount", "").strip().replace(",", ".")
    payment_type = request.form.get("payment_type", "").strip().upper()
    if payment_type not in ("CASH", "CARD"):
        payment_type = None
    try:
        amount = float(amount_raw)
    except ValueError:
        amount = None
    if amount is not None and amount > 0:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at, project_id, payment_type) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, amount, now, now, _project_id_for_tuning_order(db, order_id), payment_type),
        )
        db.commit()
        if payment_type:
            _modulkassa_fiscalize_payment(db, order, cur.lastrowid, amount, payment_type)
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/pay/<int:payment_id>/delete", methods=["POST"])
@admin_login_required
def delete_tuning_payment(order_id, payment_id):
    db = get_db()
    db.execute("DELETE FROM tuning_payments WHERE id = ? AND order_id = ?", (payment_id, order_id))
    db.execute("DELETE FROM modulkassa_receipts WHERE payment_id = ?", (payment_id,))
    db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/pay/<int:payment_id>/receipt/check", methods=["POST"])
@admin_login_required
def check_modulkassa_receipt(order_id, payment_id):
    db = get_db()
    receipt = db.execute(
        "SELECT * FROM modulkassa_receipts WHERE payment_id = ? ORDER BY id DESC LIMIT 1", (payment_id,)
    ).fetchone()
    if receipt is not None:
        _modulkassa_check_status(db, receipt)
        db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/pay/<int:payment_id>/receipt/retry", methods=["POST"])
@admin_login_required
def retry_modulkassa_receipt(order_id, payment_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    payment = db.execute(
        "SELECT * FROM tuning_payments WHERE id = ? AND order_id = ?", (payment_id, order_id)
    ).fetchone()
    if order is not None and payment is not None and payment["payment_type"]:
        _modulkassa_fiscalize_payment(db, order, payment_id, payment["amount"], payment["payment_type"])
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/internal/cron/check-modulkassa-receipts")
def cron_check_modulkassa_receipts():
    """Hit every couple of minutes by a cron job on the host — polls
    ModulKassa for any receipt still in flight (fiscalization happens
    asynchronously on the merchant's own till) and updates its status.
    Protected by CRON_SECRET, same as the other /internal/cron endpoints."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    if not _modulkassa_configured():
        return "modulkassa not configured", 503
    db = get_db()
    pending = db.execute(
        "SELECT * FROM modulkassa_receipts WHERE status IN ('queued', 'pending')"
    ).fetchall()
    for r in pending:
        _modulkassa_check_status(db, r)
    db.commit()
    return f"checked {len(pending)} receipt(s)", 200


@app.route("/tuning/<int:order_id>/notes/add", methods=["POST"])
@admin_login_required
def add_tuning_order_note(order_id):
    db = get_db()
    order = db.execute("SELECT id FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))
    text = request.form.get("text", "").strip()
    if text:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO tuning_order_notes (order_id, author_admin_id, text, created_at) VALUES (?, ?, ?, ?)",
            (order_id, session.get("admin_id"), text, now),
        )
        db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id) + "#notes")


@app.route("/tuning/<int:order_id>/notes/<int:note_id>/delete", methods=["POST"])
@admin_login_required
def delete_tuning_order_note(order_id, note_id):
    db = get_db()
    db.execute("DELETE FROM tuning_order_note_reminders WHERE note_id = ?", (note_id,))
    db.execute("DELETE FROM tuning_order_notes WHERE id = ? AND order_id = ?", (note_id, order_id))
    db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id) + "#notes")


@app.route("/tuning/<int:order_id>/notes/<int:note_id>/remind", methods=["POST"])
@admin_login_required
def add_note_reminder(order_id, note_id):
    db = get_db()
    note = db.execute(
        "SELECT id FROM tuning_order_notes WHERE id = ? AND order_id = ?", (note_id, order_id)
    ).fetchone()
    if note is None:
        return redirect(url_for("edit_tuning_order", order_id=order_id) + "#notes")

    recipient_raw = request.form.get("remind_recipient", "").strip()
    legacy_admin_id_raw = request.form.get("remind_admin_id", "").strip()
    remind_at_raw = request.form.get("remind_at", "").strip()

    remind_employee_id = None
    remind_admin_id = None
    recipient_kind, separator, recipient_id_raw = recipient_raw.partition(":")
    if separator and recipient_id_raw.isdigit():
        recipient_id = int(recipient_id_raw)
        if recipient_kind == "employee":
            employee = db.execute(
                "SELECT id FROM employees WHERE id = ? AND deleted_at IS NULL",
                (recipient_id,),
            ).fetchone()
            remind_employee_id = employee["id"] if employee else None
        elif recipient_kind == "admin":
            admin = db.execute(
                "SELECT id FROM admin_accounts WHERE id = ?", (recipient_id,)
            ).fetchone()
            remind_admin_id = admin["id"] if admin else None
    elif legacy_admin_id_raw.isdigit():
        # Backwards compatibility for a form opened before deployment.
        admin = db.execute(
            "SELECT id FROM admin_accounts WHERE id = ?",
            (int(legacy_admin_id_raw),),
        ).fetchone()
        remind_admin_id = admin["id"] if admin else None

    # Older production databases retain NOT NULL on remind_admin_id. The
    # creating administrator is a compatibility value; delivery prefers the
    # employee column whenever it is populated.
    legacy_admin_id = remind_admin_id or session.get("admin_id")
    remind_at = None
    if remind_at_raw:
        try:
            # <input type="datetime-local"> gives "YYYY-MM-DDTHH:MM" — store
            # in the same "YYYY-MM-DD HH:MM" string form used everywhere
            # else in this file, so it sorts/compares correctly as text.
            parsed = dt.datetime.strptime(remind_at_raw, "%Y-%m-%dT%H:%M")
            remind_at = parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            remind_at = None

    if (remind_employee_id or remind_admin_id) and legacy_admin_id and remind_at:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO tuning_order_note_reminders "
            "(note_id, remind_admin_id, remind_employee_id, remind_at, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (note_id, legacy_admin_id, remind_employee_id, remind_at, now),
        )
        db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id) + "#notes")


@app.route("/tuning/<int:order_id>/notes/<int:note_id>/reminders/<int:reminder_id>/cancel", methods=["POST"])
@admin_login_required
def cancel_note_reminder(order_id, note_id, reminder_id):
    db = get_db()
    db.execute(
        "DELETE FROM tuning_order_note_reminders WHERE id = ? AND note_id = ? AND sent_at IS NULL",
        (reminder_id, note_id),
    )
    db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id) + "#notes")


def _auto_writeoff_order_product(db, order_id, product_id, quantity, tuning_order_product_id):
    """If a single warehouse holds enough of this product, write that
    quantity off automatically and attribute the cost to the order's
    project — same idea as a tuningman writing off materials against an
    assigned task (team_tuning_task_writeoff_material), just triggered by
    adding a goods line to the order instead of using it on a task.

    Deliberately does nothing (no error, no partial write-off) if no
    single warehouse has enough — there's no UI here to split a write-off
    across warehouses or to pick one by hand, so "in stock" only counts
    when one place alone can cover it. Returns the warehouse row written
    off from, or None."""
    stock_row = db.execute(
        "SELECT * FROM supply_stock WHERE product_id = ? AND quantity >= ? "
        "ORDER BY quantity DESC LIMIT 1",
        (product_id, quantity),
    ).fetchone()
    if stock_row is None:
        return None
    product = db.execute("SELECT * FROM supply_products WHERE id = ?", (product_id,)).fetchone()
    project_id = _project_id_for_tuning_order(db, order_id)
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    amount = quantity * product["cost_price"]
    db.execute(
        "INSERT INTO supply_writeoffs (product_id, warehouse_id, quantity, reason, note, created_at, "
        "project_id, cost_price, amount, employee_name, tuning_order_product_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (product_id, stock_row["warehouse_id"], quantity, TUNING_GOODS_WRITEOFF_REASON, None, now,
         project_id, product["cost_price"], amount, session.get("admin_name"), tuning_order_product_id),
    )
    db.execute(
        "UPDATE supply_stock SET quantity = quantity - ? WHERE id = ?",
        (quantity, stock_row["id"]),
    )
    db.commit()
    _maybe_create_low_stock_request(db, product_id)
    return stock_row


@app.route("/tuning/<int:order_id>/products/add", methods=["POST"])
@admin_login_required
def add_tuning_order_product(order_id):
    db = get_db()
    order = db.execute("SELECT id FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))

    product_id_raw = request.form.get("product_id", "").strip()
    quantity_raw = request.form.get("quantity", "").strip().replace(",", ".")
    product_id = int(product_id_raw) if product_id_raw.isdigit() else None
    product = None
    if product_id is not None:
        product = db.execute("SELECT * FROM supply_products WHERE id = ?", (product_id,)).fetchone()

    quantity = None
    try:
        quantity = float(quantity_raw)
    except ValueError:
        pass

    if product is not None and quantity is not None and quantity > 0:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT INTO tuning_order_products (order_id, product_id, product_name, quantity, "
            "unit_price, cost_price, unit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, product_id, product["name"], quantity,
             product["sale_price"], product["cost_price"], product["cost_unit"], now),
        )
        db.commit()
        _auto_writeoff_order_product(db, order_id, product_id, quantity, cur.lastrowid)
        _recompute_order_totals(db, order_id)
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/products/<int:row_id>/remove", methods=["POST"])
@admin_login_required
def remove_tuning_order_product(order_id, row_id):
    db = get_db()
    # Undo any stock this row's auto-write-off took (see
    # _auto_writeoff_order_product) — otherwise removing a mistakenly
    # added product would leave the stock deducted with no way to get it
    # back short of a manual "Оприходовать".
    writeoff = db.execute(
        "SELECT * FROM supply_writeoffs WHERE tuning_order_product_id = ?", (row_id,)
    ).fetchone()
    if writeoff is not None:
        db.execute(
            "UPDATE supply_stock SET quantity = quantity + ? WHERE product_id = ? AND warehouse_id = ?",
            (writeoff["quantity"], writeoff["product_id"], writeoff["warehouse_id"]),
        )
        db.execute("DELETE FROM supply_writeoffs WHERE id = ?", (writeoff["id"],))
    db.execute("DELETE FROM tuning_order_products WHERE id = ? AND order_id = ?", (row_id, order_id))
    db.commit()
    _recompute_order_totals(db, order_id)
    return redirect(url_for("edit_tuning_order", order_id=order_id))


def _render_client_dashboard(db, client, viewer_role, client_section=TUNING_SEGMENT):
    """Build the shared cabinet while keeping privileged data server-side.

    The public token route always calls this with ``client``.  Only the
    admin-authenticated route may load cost prices, payment rows, internal
    notes and source metadata. Customer managers receive only excursion
    contact details and trip history.
    """
    is_admin_view = viewer_role == "admin"
    is_manager_view = viewer_role == "manager"
    is_staff_view = is_admin_view or is_manager_view
    excursion_trips = []
    if is_staff_view and client_section == EXCURSION_SEGMENT:
        excursion_trips = [
            dict(row) for row in db.execute(
                "SELECT DISTINCT si.id, si.kind, si.boat, si.service_name, "
                "si.starts_at, si.ends_at, si.status FROM schedule_items si "
                "JOIN schedule_participants sp ON sp.schedule_item_id = si.id "
                "WHERE sp.client_id = ? AND si.deleted_at IS NULL "
                "ORDER BY si.starts_at DESC, si.id DESC",
                (client["id"],),
            ).fetchall()
        ]
    order_columns = (
        "id, equipment_type, boat_model, boat_registration_number, motor_model, "
        "motor_serial_number, discount_type, discount_value, total, status, created_at"
    )
    if is_admin_view:
        order_columns += ", sale_channel, updated_at, source, source_ref"
    order_rows = []
    if not (is_manager_view and client_section == EXCURSION_SEGMENT):
        order_rows = db.execute(
            f"SELECT {order_columns} FROM tuning_orders "
            "WHERE client_id = ? ORDER BY order_date DESC, id DESC",
            (client["id"],),
        ).fetchall()

    orders = []
    paid_total = 0.0
    remaining_total = 0.0
    for o in order_rows:
        if is_admin_view:
            payments, paid_amount, remaining = _order_payment_totals(
                db, o["id"], o["total"]
            )
        else:
            payment_total = db.execute(
                "SELECT COALESCE(SUM(amount), 0) AS total "
                "FROM tuning_payments WHERE order_id = ?",
                (o["id"],),
            ).fetchone()
            payments = []
            paid_amount = payment_total["total"]
            remaining = max(0.0, o["total"] - paid_amount)
        item_columns = "id, work_name, price, status"
        if is_admin_view:
            item_columns += ", cost_price, multiplier"
        items = db.execute(
            f"SELECT {item_columns} FROM tuning_order_items "
            "WHERE order_id = ? ORDER BY id",
            (o["id"],),
        ).fetchall()
        goods_columns = "id, product_name, quantity, unit_price, unit"
        if is_admin_view:
            goods_columns += ", cost_price"
        goods_items = db.execute(
            f"SELECT {goods_columns} FROM tuning_order_products "
            "WHERE order_id = ? ORDER BY id",
            (o["id"],),
        ).fetchall()
        order = dict(o)
        order["paid_amount"] = paid_amount
        order["remaining"] = remaining
        order["work_items"] = items
        order["goods_items"] = goods_items
        order["hull_sheets"] = db.execute(
            "SELECT * FROM hull_diagnostic_sheets WHERE tuning_order_id = ? ORDER BY id", (o["id"],)
        ).fetchall()
        if is_admin_view:
            active_items = [item for item in items if item["status"] != "removed"]
            internal_cost = sum(item["cost_price"] for item in active_items)
            internal_cost += sum(
                item["quantity"] * item["cost_price"] for item in goods_items
            )
            order["internal_cost"] = internal_cost
            order["gross_margin"] = o["total"] - internal_cost
            order["payments"] = payments
            order["internal_notes"] = _order_notes(db, o["id"])
        orders.append(order)
        paid_total += paid_amount
        remaining_total += remaining

    grand_total = sum(o["total"] for o in order_rows)

    # Payment URLs are client actions, so the administrative view does not
    # even load them; it exposes only the recorded payment history instead.
    for order in orders:
        pending = None
        if not is_admin_view:
            pending = db.execute(
                "SELECT amount, confirmation_url FROM tuning_yookassa_payments "
                "WHERE order_id = ? AND status != 'succeeded' "
                "ORDER BY id DESC LIMIT 1",
                (order["id"],),
            ).fetchone()
        order["yookassa_pending"] = pending

    work_photos_by_item = {}
    for order in orders:
        for item in order["work_items"]:
            work_photos_by_item[item["id"]] = get_work_item_photos(db, item["id"])

    return render_template(
        "client_dashboard.html", client=client, orders=orders, grand_total=grand_total,
        paid_total=paid_total, remaining_total=remaining_total,
        primary_equipment=(
            tuning_equipment_label(orders[0])
            if orders else (client["boat_model"] or "—")
        ),
        work_photos_by_item=work_photos_by_item, cost_units=SUPPLY_COST_UNITS,
        client_acquisition_channels=CLIENT_ACQUISITION_CHANNELS,
        viewer_role=viewer_role,
        client_section=client_section,
        excursion_trips=excursion_trips,
        admin_name=(
            session.get("admin_name")
            if is_admin_view
            else session.get("team_employee_name") if is_manager_view else None
        ),
    )


@app.route("/client/<token>")
def client_dashboard(token):
    db = get_db()
    client = db.execute(
        "SELECT id, client_name, boat_model, token FROM clients WHERE token = ?",
        (token,),
    ).fetchone()
    if client is None:
        return redirect(url_for("home"))
    return _render_client_dashboard(db, client, "client")


@app.route("/admin/clients/<int:client_id>/cabinet")
@excursion_manager_or_admin_required
def admin_client_dashboard(client_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        if _is_customer_manager(db):
            return redirect(url_for("tuning_clients", section=EXCURSION_SEGMENT))
        return redirect(url_for("tuning_index"))
    if _is_customer_manager(db):
        is_excursion_client = db.execute(
            "SELECT 1 FROM client_segments WHERE client_id = ? AND segment = ?",
            (client_id, EXCURSION_SEGMENT),
        ).fetchone() is not None
        if not is_excursion_client:
            return redirect(url_for("tuning_clients", section=EXCURSION_SEGMENT))
        return _render_client_dashboard(db, client, "manager", EXCURSION_SEGMENT)
    client_section = request.args.get("section", TUNING_SEGMENT)
    if client_section not in (TUNING_SEGMENT, EXCURSION_SEGMENT):
        client_section = TUNING_SEGMENT
    return _render_client_dashboard(db, client, "admin", client_section)


@app.route(
    "/admin/clients/<int:client_id>/acquisition-channel", methods=["POST"]
)
@excursion_manager_or_admin_required
def update_client_acquisition_channel(client_id):
    db = get_db()
    channel = request.form.get("acquisition_channel", "").strip()
    allowed_channels = {
        item["value"] for item in CLIENT_ACQUISITION_CHANNELS
    }
    is_excursion_client = db.execute(
        "SELECT 1 FROM client_segments WHERE client_id = ? AND segment = ?",
        (client_id, EXCURSION_SEGMENT),
    ).fetchone() is not None
    if is_excursion_client and (not channel or channel in allowed_channels):
        db.execute(
            "UPDATE clients SET acquisition_channel = ? WHERE id = ?",
            (channel, client_id),
        )
        db.commit()
    return redirect(url_for(
        "admin_client_dashboard", client_id=client_id,
        section=EXCURSION_SEGMENT,
    ))


@app.route("/client/<token>/item/<int:item_id>/approve", methods=["POST"])
def client_approve_item(token, item_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE token = ?", (token,)).fetchone()
    if client is None:
        return redirect(url_for("home"))
    item = db.execute(
        "SELECT toi.id, toi.status, toi.work_name, o.client_name, "
        "o.equipment_type, o.boat_model, o.boat_registration_number, "
        "o.motor_model, o.motor_serial_number "
        "FROM tuning_order_items toi "
        "JOIN tuning_orders o ON o.id = toi.order_id "
        "WHERE toi.id = ? AND o.client_id = ?",
        (item_id, client["id"]),
    ).fetchone()
    if item is not None and item["status"] == "pending":
        db.execute("UPDATE tuning_order_items SET status = 'approved' WHERE id = ?", (item_id,))
        db.commit()
        equipment_label = tuning_equipment_label(item)
        dispatch_notification(
            db,
            EVENT_TUNING_WORK_APPROVED,
            f"✅ Клиент согласовал работу\n"
            f"Клиент: {html.escape(item['client_name'])} ({html.escape(equipment_label)})\n"
            f"Работа: {html.escape(item['work_name'])}",
            send_telegram_notification,
            fallback_chat_id=TELEGRAM_APPROVAL_CHAT_ID,
        )
        send_push_notification(
            "Клиент согласовал работу",
            f"{item['client_name']} ({equipment_label}) — {item['work_name']}",
            url="/tuning",
        )
    return redirect(url_for("client_dashboard", token=token))


@app.route("/client/<token>/hull-sheet/<int:sheet_id>")
def client_hull_diagnostic_sheet(token, sheet_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE token = ?", (token,)).fetchone()
    if client is None:
        return redirect(url_for("home"))
    sheet = db.execute(
        "SELECT s.* FROM hull_diagnostic_sheets s "
        "JOIN tuning_orders o ON o.id = s.tuning_order_id "
        "WHERE s.id = ? AND o.client_id = ?",
        (sheet_id, client["id"]),
    ).fetchone()
    if sheet is None:
        return redirect(url_for("client_dashboard", token=token))

    defects = db.execute(
        "SELECT * FROM hull_diagnostic_defects WHERE sheet_id = ? ORDER BY id", (sheet_id,)
    ).fetchall()
    defects_by_view = {v: [] for v in HULL_VIEWS}
    for d in defects:
        if d["view"] in defects_by_view:
            defects_by_view[d["view"]].append(d)

    return render_template(
        "hull_diagnostic_sheet_client.html", sheet=sheet, defects_by_view=defects_by_view,
        defects=defects, token=token,
    )


# =======================================================================
# ЮKassa — оплата заказов тюнинг-центра
# =======================================================================

def yookassa_configured():
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)


def _normalize_ru_phone(phone):
    """ЮKassa требует номер в формате ITU-T E.164 (только цифры, например
    79000000000) — а в заказах телефон вводится в свободной форме."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


def _yookassa_request(method, path, json_body=None, idempotence_key=None, params=None):
    headers = {}
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    resp = requests.request(
        method, f"{YOOKASSA_API_BASE}{path}",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        json=json_body, params=params, headers=headers, timeout=15,
    )
    if not resp.ok:
        message = f"ЮKassa вернула {resp.status_code}: {resp.text[:500]}"
        # HTTP 500 is explicitly an unknown outcome in the YooKassa API:
        # the operation may have completed even though the answer was lost.
        # Keep it in the RequestException branch so refund code retries only
        # with the original idempotence key instead of permitting a new one.
        if resp.status_code >= 500:
            raise requests.HTTPError(message, response=resp)
        raise RuntimeError(message)
    return resp.json()


def _sync_yookassa_payment(db, record, remote=None):
    """Refresh a stored ЮKassa payment's status from the API (or from an
    already-fetched `remote` dict) and, the first time it turns succeeded,
    record it in the same tuning_payments ledger admin-entered payments use
    — so paid/remaining totals everywhere stay correct without special-casing
    online payments."""
    if remote is None:
        remote = _yookassa_request("GET", f"/payments/{record['yookassa_payment_id']}")
    status = remote.get("status", record["status"])
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "UPDATE tuning_yookassa_payments SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, record["id"]),
    )
    if status == "succeeded" and not record["tuning_payment_id"]:
        cur = db.execute(
            "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at, project_id) VALUES (?, ?, ?, ?, ?)",
            (record["order_id"], record["amount"], now, now, _project_id_for_tuning_order(db, record["order_id"])),
        )
        db.execute(
            "UPDATE tuning_yookassa_payments SET tuning_payment_id = ? WHERE id = ?",
            (cur.lastrowid, record["id"]),
        )
    db.commit()
    return status


@app.route("/tuning/<int:order_id>/yookassa/create", methods=["POST"])
@admin_login_required
def create_yookassa_payment(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None or not yookassa_configured():
        return redirect(url_for("tuning_index"))

    amount_raw = request.form.get("amount", "").strip().replace(",", ".")
    try:
        amount = round(float(amount_raw), 2)
    except ValueError:
        amount = None
    if amount is None or amount <= 0:
        session["yookassa_error"] = "Укажите сумму счёта — больше нуля."
        return redirect(url_for("edit_tuning_order", order_id=order_id))

    client = db.execute("SELECT * FROM clients WHERE id = ?", (order["client_id"],)).fetchone()
    return_url = (
        url_for("client_dashboard", token=client["token"], _external=True)
        if client else url_for("home", _external=True)
    )

    body = {
        "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
        "capture": True,
        "description": f"Заказ №{order_id} — {order['client_name']}"[:128],
        "confirmation": {"type": "redirect", "return_url": return_url},
        "metadata": {"tuning_order_id": str(order_id)},
        "receipt": {
            "customer": {"phone": _normalize_ru_phone(order["phone"])},
            "items": [
                {
                    "description": f"Оплата заказа №{order_id} в тюнинг-центре"[:128],
                    "quantity": 1,
                    "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
                    "vat_code": YOOKASSA_RECEIPT_VAT_CODE,
                    "measure": "piece",
                    "payment_subject": "service",
                    "payment_mode": "full_payment",
                }
            ],
        },
    }
    try:
        remote = _yookassa_request(
            "POST", "/payments", json_body=body, idempotence_key=secrets.token_hex(16)
        )
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO tuning_yookassa_payments (order_id, yookassa_payment_id, amount, status, "
            "confirmation_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (order_id, remote["id"], amount, remote.get("status", "pending"),
             remote["confirmation"]["confirmation_url"], now, now),
        )
        db.commit()
    except Exception as e:
        session["yookassa_error"] = str(e)
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/yookassa/<int:payment_id>/check", methods=["POST"])
@admin_login_required
def check_yookassa_payment(order_id, payment_id):
    db = get_db()
    record = db.execute(
        "SELECT * FROM tuning_yookassa_payments WHERE id = ? AND order_id = ?",
        (payment_id, order_id),
    ).fetchone()
    if record is not None:
        try:
            _sync_yookassa_payment(db, record)
        except Exception:
            pass
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/yookassa/<int:payment_id>/delete", methods=["POST"])
@admin_login_required
def delete_yookassa_payment(order_id, payment_id):
    """Undo an erroneous invoice. A succeeded payment is real money already
    received, so it's kept — deletion is for invoices that shouldn't have
    existed (wrong amount, created by mistake, client changed their mind).
    waiting_for_capture means ЮKassa is holding an authorization on the
    client's card; that has to be actually canceled through their API
    before we forget about it locally, or the hold could still get
    captured later with nothing here to show for it. pending/canceled
    payments never captured anything, so there's nothing to cancel — just
    drop the local record."""
    db = get_db()
    record = db.execute(
        "SELECT * FROM tuning_yookassa_payments WHERE id = ? AND order_id = ?",
        (payment_id, order_id),
    ).fetchone()
    if record is None:
        return redirect(url_for("edit_tuning_order", order_id=order_id))
    if record["status"] == "succeeded":
        session["yookassa_error"] = "Нельзя удалить счёт с успешной оплатой."
        return redirect(url_for("edit_tuning_order", order_id=order_id))
    if record["status"] == "waiting_for_capture":
        try:
            _yookassa_request(
                "POST", f"/payments/{record['yookassa_payment_id']}/cancel",
                json_body={}, idempotence_key=secrets.token_hex(16),
            )
        except Exception as e:
            session["yookassa_error"] = f"Не удалось отменить счёт в ЮKassa: {e}"
            return redirect(url_for("edit_tuning_order", order_id=order_id))
    db.execute("DELETE FROM tuning_yookassa_payments WHERE id = ?", (payment_id,))
    db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/yookassa/webhook", methods=["POST"])
def yookassa_webhook():
    # No auth — ЮKassa calls this directly. We never trust the notification
    # body for the actual status: we re-fetch the payment from the API by
    # its id before recording anything, per ЮKassa's own recommendation.
    try:
        payload = request.get_json(force=True, silent=True) or {}
        object_id = (payload.get("object") or {}).get("id")
        event = str(payload.get("event") or "")
        if object_id:
            db = get_db()
            if event.startswith("refund."):
                remote_refund = _yookassa_request("GET", f"/refunds/{object_id}")
                refund_services.apply_remote_refund(db, remote_refund)
            else:
                record = db.execute(
                    "SELECT * FROM tuning_yookassa_payments WHERE yookassa_payment_id = ?",
                    (object_id,),
                ).fetchone()
                if record is not None:
                    _sync_yookassa_payment(db, record)
                excursion_payment = db.execute(
                    "SELECT 1 FROM excursion_yookassa_payments "
                    "WHERE yookassa_payment_id = ?",
                    (object_id,),
                ).fetchone()
                if excursion_payment is not None:
                    remote_payment = _yookassa_request("GET", f"/payments/{object_id}")
                    refund_services.sync_remote_payment(db, remote_payment)
    except Exception:
        pass
    return "", 200


# =======================================================================
# Импорт рейсов из Yclients
# =======================================================================

YCLIENTS_API_BASE = "https://api.yclients.com/api/v1"


def yclients_configured():
    return bool(YCLIENTS_PARTNER_TOKEN and YCLIENTS_USER_TOKEN and YCLIENTS_COMPANY_ID)


def _yclients_headers():
    return {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {YCLIENTS_PARTNER_TOKEN}, User {YCLIENTS_USER_TOKEN}",
    }


def yclients_get_records(start_date, end_date):
    """Fetch every record (booking) in the given date range, paginating as
    needed. Returns a list of raw record dicts from the Yclients API."""
    headers = _yclients_headers()
    all_records = []
    page = 1
    while True:
        resp = requests.get(
            f"{YCLIENTS_API_BASE}/records/{YCLIENTS_COMPANY_ID}",
            headers=headers,
            params={"start_date": start_date, "end_date": end_date, "page": page, "count": 100},
            timeout=20,
        )
        if not resp.ok:
            raise RuntimeError(
                f"Yclients вернул {resp.status_code} для {resp.url}. "
                f"Ответ сервера: {resp.text[:500]}"
            )
        body = resp.json()
        if not body.get("success"):
            raise RuntimeError("Yclients API вернул success=false: " + json.dumps(body)[:300])
        records = body.get("data") or []
        all_records.extend(records)
        meta = body.get("meta") or {}
        total_count = meta.get("total_count", len(all_records))
        if len(all_records) >= total_count or not records:
            break
        page += 1
        if page > 50:  # safety valve
            break
    return all_records


def yclients_get_staff():
    """Return the current YCLIENTS staff directory for this branch.

    The endpoint is occasionally slow even when the records endpoint is
    healthy. One immediate retry prevents an otherwise successful import
    from losing every schedule-only minimum-rate top-up.
    """
    last_error = None
    for attempt in range(2):
        try:
            resp = requests.get(
                f"{YCLIENTS_API_BASE}/staff/{YCLIENTS_COMPANY_ID}",
                headers=_yclients_headers(),
                timeout=20,
            )
            if not resp.ok:
                raise RuntimeError(
                    f"Yclients вернул {resp.status_code} при загрузке сотрудников. "
                    f"Ответ сервера: {resp.text[:500]}"
                )
            body = resp.json()
            if not body.get("success"):
                raise RuntimeError(
                    "Yclients API вернул success=false при загрузке сотрудников: "
                    + json.dumps(body)[:300]
                )
            staff = body.get("data") or []
            if not isinstance(staff, list):
                raise RuntimeError("Yclients API вернул некорректный список сотрудников.")
            return staff
        except (requests.RequestException, RuntimeError, ValueError) as error:
            last_error = error
            if attempt == 0:
                time.sleep(0.25)
    raise last_error


def yclients_get_working_staff_days(employee_names, start_date, end_date):
    """Read real work schedules, including shifts that have zero bookings.

    ``records`` cannot reveal an entirely empty shift. The schedule endpoint
    is queried for every active local employee whose name exists in the
    YCLIENTS staff directory. Requests are independent and run concurrently.
    The result uses canonical local names so payroll entries remain linked to
    the employee directory even if YCLIENTS differs only in letter case.
    """
    local_names = {
        name.strip().casefold(): name.strip() for name in employee_names if name.strip()
    }
    staff_matches = []
    for staff in yclients_get_staff():
        yclients_name = str(staff.get("name") or "").strip()
        local_name = local_names.get(yclients_name.casefold())
        fired = str(staff.get("fired") or "0").strip().lower() in {"1", "true", "yes"}
        if local_name and staff.get("id") is not None and not fired:
            staff_matches.append((staff["id"], local_name))

    if not staff_matches:
        return {
            "staffed_days": set(),
            "checked_employees": set(),
            "failed_employees": [],
        }

    def fetch_one(match):
        staff_id, local_name = match
        last_error = None
        for attempt in range(2):
            try:
                resp = session.get(
                    f"{YCLIENTS_API_BASE}/schedule/{YCLIENTS_COMPANY_ID}/{staff_id}/"
                    f"{start_date}/{end_date}",
                    timeout=20,
                )
                if not resp.ok:
                    raise RuntimeError(
                        f"HTTP {resp.status_code}: {resp.text[:200]}"
                    )
                body = resp.json()
                if not body.get("success") or not isinstance(body.get("data"), list):
                    raise RuntimeError("некорректный ответ API")
                working_dates = set()
                for day in body["data"]:
                    work_date = str(day.get("date") or "")[:10]
                    is_working = str(day.get("is_working") or "0").strip().lower()
                    if (
                        start_date <= work_date <= end_date
                        and is_working in {"1", "true", "yes"}
                    ):
                        working_dates.add(work_date)
                return local_name, working_dates, None
            except (requests.RequestException, RuntimeError, ValueError) as error:
                last_error = error
                if attempt == 0:
                    time.sleep(0.25)
        return local_name, set(), str(last_error)

    staffed_days = set()
    checked_employees = set()
    failed_employees = []
    with requests.Session() as session:
        session.headers.update(_yclients_headers())
        with ThreadPoolExecutor(max_workers=min(10, len(staff_matches))) as pool:
            for employee, working_dates, error in pool.map(fetch_one, staff_matches):
                if error is not None:
                    failed_employees.append(employee)
                    print(
                        f"YCLIENTS schedule failed for {employee}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                checked_employees.add(employee)
                staffed_days.update((employee, work_date) for work_date in working_dates)
    return {
        "staffed_days": staffed_days,
        "checked_employees": checked_employees,
        "failed_employees": sorted(failed_employees),
    }


def yclients_get_activity_colors(activity_ids):
    """Group events (activities) carry their own color on the event object
    itself, not on each individual record inside it — fetch each distinct
    event once and return {activity_id: color}. Fetch failures for a single
    event are skipped rather than aborting the whole import (that event's
    boat will just stay unmatched for manual selection).

    One request per activity, but they're all independent reads, so they go
    out concurrently instead of one-by-one — with a week's worth of imports
    easily touching 20-30 activities, this was the slowest part of the
    whole import by far (a serial loop of one network round-trip each)."""
    activity_ids = list(activity_ids)
    if not activity_ids:
        return {}

    def fetch_one(activity_id):
        try:
            resp = session.get(
                f"{YCLIENTS_API_BASE}/activity/{YCLIENTS_COMPANY_ID}/{activity_id}",
                timeout=20,
            )
            if not resp.ok:
                return activity_id, None
            body = resp.json()
            data = body.get("data") or {}
            return activity_id, data.get("color")
        except requests.RequestException:
            return activity_id, None

    colors = {}
    with requests.Session() as session:
        session.headers.update(_yclients_headers())
        with ThreadPoolExecutor(max_workers=min(10, len(activity_ids))) as pool:
            for activity_id, color in pool.map(fetch_one, activity_ids):
                if color:
                    colors[activity_id] = color
    return colors


def _normalize_color(value):
    """Normalize a hex color for comparison: lowercase, no leading '#'
    (Yclients returns colors without '#', so this avoids false mismatches)."""
    return (value or "").strip().lower().lstrip("#")


def _yclients_record_color(rec):
    return _normalize_color(rec.get("custom_color") or rec.get("color"))


def _yclients_record_is_blocker(rec):
    """A manager's red "don't schedule this person" placeholder — not a
    trip, and not a real shift for the minimum-rate top-up either."""
    return _yclients_record_color(rec) == BLOCKED_SHIFT_COLOR


def _yclients_record_datetime(rec):
    return (rec.get("datetime") or rec.get("date") or "").strip()


def _yclients_slot_datetime(rec):
    """Normalized 'YYYY-MM-DDTHH:MM' for grouping purposes — deliberately
    drops seconds/timezone-representation differences, since two records
    for the very same physical trip should still match even if Yclients
    formats their timestamps slightly differently (extra seconds, a
    trailing .000000, etc.)."""
    raw = _yclients_record_datetime(rec)
    if not raw:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(raw)
        return parsed.strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return raw[:16]  # best-effort fallback: date + "T" + HH:MM


def _yclients_group_key(rec):
    """Group by Yclients' own activity_id when present (real group events).
    Otherwise, group by color + start time rounded to the minute: this is
    how a captain and a guide working the same trip end up as two separate
    individual records (one with the real client/price, one an empty
    placeholder for the second staff member) — same boat color, same slot,
    no shared activity_id."""
    activity_id = rec.get("activity_id")
    if activity_id:
        return f"activity:{activity_id}"
    color = _yclients_record_color(rec)
    when = _yclients_slot_datetime(rec)
    if color and when:
        return f"slot:{color}:{when}"
    return f"record:{rec.get('id')}"


def _yclients_activity_color(activity_id, activity_colors):
    """Return a normalized activity colour for either string or numeric ids."""
    color = activity_colors.get(activity_id)
    activity_id_raw = str(activity_id or "")
    if color is None:
        color = activity_colors.get(activity_id_raw)
    if color is None and activity_id_raw.isdigit():
        color = activity_colors.get(int(activity_id_raw))
    return _normalize_color(color)


def _yclients_group_is_cancelled(group_key, records, activity_colors):
    """YCLIENTS marks cancelled trips/activities with its red colour."""
    if group_key.startswith("activity:"):
        activity_id = group_key.split(":", 1)[1]
        return (
            _yclients_activity_color(activity_id, activity_colors)
            == CANCELLED_TRIP_COLOR
        )
    return any(
        _yclients_record_color(record) == CANCELLED_TRIP_COLOR
        for record in records
    )


def _yclients_record_date(rec):
    raw = rec.get("date") or (rec.get("datetime") or "")[:10]
    try:
        return dt.date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return dt.date.today().isoformat()


def _yclients_record_time(rec):
    """Extract just the HH:MM start time from the record's datetime, for
    display in the import candidates list."""
    raw = rec.get("datetime") or ""
    try:
        return raw[11:16] if len(raw) >= 16 else ""
    except (TypeError, IndexError):
        return ""


def _yclients_record_hours(rec):
    seconds = rec.get("seance_length") or rec.get("length") or 0
    if seconds:
        return round(seconds / 3600, 2)
    return None


def _yclients_record_revenue(rec):
    return sum(float(s.get("cost") or 0) for s in (rec.get("services") or []))


def build_import_candidates(records, activity_colors=None):
    """Group raw Yclients records into trip candidates:
    - all bookings sharing an activity_id (a real group event with several
      independent attendees) become one trip, revenue summed across all of
      them; the boat is determined by the *group event's own* color (passed
      in via activity_colors), since individual records inside it don't
      carry a color of their own;
    - individual records with no activity_id but the same color + exact
      start time become one trip too (this is how two staff — e.g. captain
      and guide — are split across a "real" record and an empty placeholder
      record for the same physical trip): revenue is taken from whichever
      record actually carries a price, NOT summed, to avoid double-counting
      the same single sale;
    - anything else stays its own trip.
    Boat/channel/commission are guessed from the color and comment fields.
    """
    activity_colors = activity_colors or {}
    groups = {}
    for rec in records:
        if rec.get("deleted"):
            continue
        key = _yclients_group_key(rec)
        groups.setdefault(key, []).append(rec)

    candidates = []
    for key, recs in groups.items():
        if _yclients_group_is_cancelled(key, recs, activity_colors):
            continue
        is_activity_group = key.startswith("activity:")
        trip_date = _yclients_record_date(recs[0])
        trip_time = _yclients_record_time(recs[0])

        # Boat: for a real group event, the color lives on the event object
        # itself (looked up by activity_id); for individual/slot-merged
        # records, try each record's own color until one matches.
        boat = None
        raw_color_seen = ""
        if is_activity_group:
            activity_id_raw = key.split(":", 1)[1]
            color = _yclients_activity_color(activity_id_raw, activity_colors)
            raw_color_seen = color
            for c, b in BOAT_COLORS.items():
                if _normalize_color(c) == color:
                    boat = b
                    break
        else:
            for r in recs:
                color = _yclients_record_color(r)
                if color and not raw_color_seen:
                    raw_color_seen = color
                for c, b in BOAT_COLORS.items():
                    if _normalize_color(c) == color:
                        boat = b
                        break
                if boat:
                    break

        # The booked service only ever lives on ONE record per trip — usually
        # whichever one carries the price (e.g. the captain's) — a second
        # crew member's own record (e.g. the guide's) comes back from
        # Yclients with an empty services list even though they're a real,
        # named participant. So the service/вид рейса is resolved ONCE per
        # trip from whichever record actually has it, then applied to every
        # crew member — looking it up per-record would leave every other
        # staff member's row without a вид рейса (and so without an hourly
        # rate), which fails validation.
        group_service = next(
            (((r.get("services") or [None])[0]) for r in recs if r.get("services")), None
        )
        group_service_id = group_service.get("id") if group_service else None
        group_title_raw = group_service.get("title", "") if group_service else ""
        title = (
            YCLIENTS_SERVICE_ID_TO_WORK_TYPE.get(group_service_id)
            or YCLIENTS_SERVICE_TO_WORK_TYPE.get(group_title_raw)
            or group_title_raw
        )
        wt = next((w for w in WORK_TYPES if w["name"] == title), None)

        # Labor rows: one per distinct staff member, hours from seance
        # length when available (falls back to the matched вид рейса's
        # default hours).
        labor_items = []
        seen_staff = set()
        for r in recs:
            staff_name = (r.get("staff") or {}).get("name", "").strip()
            if not staff_name:
                # An empty placeholder record for a second staff slot that
                # nobody was actually assigned to in Yclients — not a real
                # crew member, so it shouldn't become a labor row with a
                # blank name (that would fail validation on its own, and
                # would also stop the solo-crew rate rule below from ever
                # firing since it counts distinct labor rows).
                continue
            hours = _yclients_record_hours(r) or (wt["hours"] if wt else "")
            rate = wt["rate"] if wt else ""
            dedup_key = (staff_name, title)
            if dedup_key in seen_staff:
                continue
            seen_staff.add(dedup_key)
            labor_items.append({
                "employee": staff_name, "work_type": title,
                "quantity": hours, "rate": rate,
            })
        if not labor_items:
            labor_items = [{"employee": "", "work_type": "", "quantity": "", "rate": ""}]

        if is_activity_group:
            # Real group event: every record is a separate paying attendee —
            # sum their revenue and blend the channel/commission accordingly.
            revenue = sum(_yclients_record_revenue(r) for r in recs)
            channels = set()
            agg_revenue = 0.0
            direct_revenue = 0.0
            notes = []
            for r in recs:
                comment = (r.get("comment") or "").strip()
                rec_revenue = _yclients_record_revenue(r)
                if comment:
                    channels.add("aggregator")
                    agg_revenue += rec_revenue
                    notes.append(comment)
                else:
                    channels.add("direct")
                    direct_revenue += rec_revenue
            boat_info = boat_lookup(boat) if boat else None
            if len(channels) > 1:
                sale_channel = "mixed"
                commission_pct = round(
                    (direct_revenue * boat_info["commission_direct"] +
                     agg_revenue * boat_info["commission_aggregator"]) / revenue, 1
                ) if boat_info and revenue else ""
            elif channels == {"aggregator"}:
                sale_channel = "aggregator"
                commission_pct = boat_info["commission_aggregator"] if boat_info else ""
            else:
                sale_channel = "direct"
                commission_pct = boat_info["commission_direct"] if boat_info else ""
            note = " / ".join(notes)
        else:
            # Same physical trip split across several staff records (one real
            # + one or more empty placeholders) — there is only ONE sale, so
            # take the price and comment from whichever record actually
            # carries it instead of summing everything.
            primary = max(recs, key=_yclients_record_revenue)
            revenue = _yclients_record_revenue(primary)
            comment = (primary.get("comment") or "").strip()
            boat_info = boat_lookup(boat) if boat else None
            if comment:
                sale_channel = "aggregator"
                commission_pct = boat_info["commission_aggregator"] if boat_info else ""
            else:
                sale_channel = "direct"
                commission_pct = boat_info["commission_direct"] if boat_info else ""
            note = comment

        # Один человек на рейсе — по умолчанию считаем, что он выполнял и
        # роль гида, и капитана (ставка 1870₽/ч), независимо от того,
        # событие это или обычная запись — комментарий с «без экскурсии»
        # решает, а не тип брони. Исключение: если в комментарии явно
        # указано «без экскурсии» — это чисто капитанская ставка (1100₽/ч).
        if len(labor_items) == 1:
            if "без экскурсии" in note.lower():
                labor_items[0]["rate"] = 1100
            else:
                labor_items[0]["rate"] = 1870

        payload = {
            "boat": boat or "",
            "trip_date": trip_date,
            "trip_time": trip_time,
            "labor_items": labor_items,
            "revenue": revenue,
            "sale_channel": sale_channel,
            "commission_pct": commission_pct,
            "fuel_cost": boat_info["fuel"] if boat_info else "",
            "mooring_cost": boat_info["mooring"] if boat_info else "",
            "note": note,
        }
        employees_label = ", ".join(i["employee"] for i in labor_items if i["employee"]) or "—"
        if boat:
            boat_label = boat
        elif raw_color_seen:
            boat_label = f"катер не определён (цвет в Yclients: {raw_color_seen})"
        else:
            boat_label = "катер не определён (цвет не задан)"
        trip_date_label = format_ru_date(trip_date)
        when_label = f"{trip_date_label} {trip_time}".strip() if trip_time else trip_date_label
        summary = f"{when_label} · {boat_label} · {employees_label} · {format_money(revenue)} ₽"
        candidates.append({"yclients_ref": key, "summary": summary, "payload": payload})
    candidates.sort(key=lambda c: (c["payload"]["trip_date"], c["payload"]["trip_time"]), reverse=True)
    return candidates


def apply_minimum_shift_rate(
    db,
    records,
    scheduled_staff_days=None,
    checked_schedule_employees=None,
    schedule_start_date=None,
    schedule_end_date=None,
):
    """Every crew member has a guaranteed minimum of MIN_SHIFT_RATE per
    shift. "Staffed" is the union of real YCLIENTS work schedules and raw
    records. The schedule source is essential for employees whose shift has
    zero bookings; records remain a fallback and also cover names outside the
    local employee directory. Earnings come from our own ``entries`` and a
    shortfall receives a top-up.

    Self-correcting: run again after trips for that day change and an
    existing top-up shrinks, grows, or disappears to match. When a complete
    schedule range was fetched, top-ups for locally checked employees who are
    no longer scheduled are removed too. Returns how many top-up entries were
    added/changed/removed.
    """
    staffed_days = set(scheduled_staff_days or ())
    real_record_days = set()
    blocked_days = set()
    for r in records:
        if r.get("deleted"):
            continue
        name = (r.get("staff") or {}).get("name", "").strip()
        if not name:
            continue
        pair = (name, _yclients_record_date(r))
        if _yclients_record_is_blocker(r):
            blocked_days.add(pair)
            continue
        real_record_days.add(pair)
        staffed_days.add(pair)

    # A red "не ставить в рейсы" marker overrides a nominal schedule day
    # only when there is no genuine record proving that the person actually
    # worked that date.
    staffed_days.difference_update(blocked_days - real_record_days)

    changed = 0
    checked_schedule_employees = set(checked_schedule_employees or ())
    if (
        checked_schedule_employees
        and schedule_start_date
        and schedule_end_date
    ):
        existing_topups = db.execute(
            "SELECT id, employee, work_date FROM entries WHERE work_type = ? "
            "AND work_date BETWEEN ? AND ?",
            (MIN_SHIFT_TOPUP_WORK_TYPE, schedule_start_date, schedule_end_date),
        ).fetchall()
        for topup in existing_topups:
            pair = (topup["employee"], topup["work_date"])
            if topup["employee"] in checked_schedule_employees and pair not in staffed_days:
                db.execute("DELETE FROM entries WHERE id = ?", (topup["id"],))
                changed += 1

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for employee, work_date in staffed_days:
        earned = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM entries "
            "WHERE employee = ? AND work_date = ? AND work_type != ?",
            (employee, work_date, MIN_SHIFT_TOPUP_WORK_TYPE),
        ).fetchone()["total"]
        needed = round(MIN_SHIFT_RATE - earned, 2)

        existing = db.execute(
            "SELECT id, amount FROM entries WHERE employee = ? AND work_date = ? AND work_type = ?",
            (employee, work_date, MIN_SHIFT_TOPUP_WORK_TYPE),
        ).fetchone()

        if needed <= 0:
            if existing:
                db.execute("DELETE FROM entries WHERE id = ?", (existing["id"],))
                changed += 1
            continue

        if existing:
            if abs(existing["amount"] - needed) > 0.01:
                db.execute(
                    "UPDATE entries SET rate = ?, amount = ? WHERE id = ?",
                    (needed, needed, existing["id"]),
                )
                changed += 1
        else:
            db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (employee, MIN_SHIFT_TOPUP_WORK_TYPE, needed, 1, needed, work_date, now),
            )
            changed += 1

    db.commit()
    return changed


def _yclients_payroll_schedule(db, start_date, end_date, today=None):
    """Fetch the authoritative non-future schedule window for payroll."""
    today = today or dt.date.today()
    start = dt.date.fromisoformat(start_date)
    end = min(dt.date.fromisoformat(end_date), today)
    if start > end:
        return {
            "staffed_days": set(),
            "checked_employees": set(),
            "failed_employees": [],
            "start_date": None,
            "end_date": None,
        }
    result = yclients_get_working_staff_days(
        _active_employee_names(db), start.isoformat(), end.isoformat()
    )
    return {
        **result,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def _is_number(value):
    if isinstance(value, (int, float)):
        return True
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _repair_boat_derived_numbers(db, rows):
    """A candidate can end up with a resolved boat but blank/invalid
    fuel_cost/mooring_cost/commission_pct — e.g. it was merged (by an
    earlier, buggier version of merge_pending_candidates, or any future
    bug like it) from a boat-less partner that revenue-sorted first, and
    its boat-derived numbers were never backfilled. Heal any such row on
    every run, independent of whether it's merging with anything *this*
    time — otherwise a candidate stuck this way before the fix shipped
    stays stuck forever, since it's alone in its slot from here on and
    the merge loop below only touches groups of 2+."""
    for row in rows:
        payload = json.loads(row["payload"])
        boat = payload.get("boat") or ""
        if not boat:
            continue
        if (
            _is_number(payload.get("fuel_cost"))
            and _is_number(payload.get("mooring_cost"))
            and _is_number(payload.get("commission_pct"))
        ):
            continue
        boat_info = boat_lookup(boat)
        if not boat_info:
            continue
        payload["fuel_cost"] = boat_info["fuel"]
        payload["mooring_cost"] = boat_info["mooring"]
        payload["commission_pct"] = (
            boat_info["commission_aggregator"]
            if payload.get("sale_channel") == "aggregator"
            else boat_info["commission_direct"]
        )
        db.execute(
            "UPDATE import_candidates SET payload = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), row["id"]),
        )


def _candidate_is_crew_placeholder(payload):
    """YCLIENTS' second-employee row: no sale and no service of its own."""
    labor_items = payload.get("labor_items") or []
    return bool(
        not (payload.get("revenue") or 0)
        and labor_items
        and not any(item.get("work_type") for item in labor_items)
    )


def merge_pending_candidates(db):
    """Second line of defence, independent of Yclients' own data: scan every
    candidate currently waiting for confirmation and merge any that share
    the same resolved boat + trip date + start time (minute precision) —
    this is the same "one real record + one empty placeholder for the
    second staff member" situation, just detected on our own already-parsed
    data instead of relying on matching raw Yclients fields exactly.
    Returns how many candidates were merged away."""
    rows = db.execute(
        "SELECT * FROM import_candidates ORDER BY id ASC"
    ).fetchall()
    _repair_boat_derived_numbers(db, rows)
    rows = db.execute(
        "SELECT * FROM import_candidates ORDER BY id ASC"
    ).fetchall()

    groups = {}
    unresolved = []
    for row in rows:
        payload = json.loads(row["payload"])
        boat = payload.get("boat") or ""
        if not boat:
            # This staff member's own Yclients record never carried a boat
            # color (that's the "empty placeholder" record — see
            # _yclients_group_key) — set aside for the date+time fallback
            # below instead of guessing a boat from nothing.
            unresolved.append((row, payload))
            continue
        slot = (boat, payload.get("trip_date", ""), payload.get("trip_time", ""))
        groups.setdefault(slot, []).append((row, payload))

    # A boat-less candidate can still be matched up by date+time alone, but
    # only when it is the zero-revenue, service-less crew placeholder that
    # YCLIENTS creates for the second employee.  A real paid booking whose
    # activity colour temporarily failed to load may belong to another boat
    # departing at the same time; merging that booking used to turn two solo
    # guide-captains into one two-person crew and incorrectly reduce both
    # rates from 1870 to 1100.
    slots_by_datetime = {}
    for slot in groups:
        boat, trip_date, trip_time = slot
        slots_by_datetime.setdefault((trip_date, trip_time), []).append(slot)
    for row, payload in unresolved:
        if not _candidate_is_crew_placeholder(payload):
            continue
        when = (payload.get("trip_date", ""), payload.get("trip_time", ""))
        matches = slots_by_datetime.get(when) or []
        if len(matches) == 1:
            groups[matches[0]].append((row, payload))

    merged_away = 0
    for slot, items in groups.items():
        if len(items) < 2:
            continue

        # One physical trip may have one paid/service-bearing booking plus
        # any number of empty crew placeholders.  Two independent paid
        # bookings at the same time are not proof of one crew, even when a
        # temporarily wrong activity colour makes their boats look equal.
        anchors = [
            item for item in items
            if not _candidate_is_crew_placeholder(item[1])
        ]
        if len(anchors) != 1:
            continue

        items.sort(key=lambda ip: ip[1].get("revenue") or 0, reverse=True)
        keep_row, keep_payload = items[0]

        labor_items = list(keep_payload.get("labor_items") or [])
        seen = {(i.get("employee"), i.get("work_type")) for i in labor_items}
        notes = [keep_payload.get("note", "")] if keep_payload.get("note") else []
        # Every yclients_ref that gets folded into this one needs to be
        # remembered (see below) — otherwise, once this trip is imported,
        # only the *kept* ref is marked as done. The merged-away one's raw
        # Yclients record is still real and still there, so next fetch it
        # comes back as a brand-new, now-partnerless candidate — this is
        # exactly the "исчезнувший рейс" junk that kept resurfacing.
        merged_refs = list(keep_payload.get("merged_refs") or [])

        for row, payload in items[1:]:
            for item in (payload.get("labor_items") or []):
                dedup_key = (item.get("employee"), item.get("work_type"))
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                labor_items.append(item)
            if payload.get("note"):
                notes.append(payload["note"])
            merged_refs.append(row["yclients_ref"])
            merged_refs.extend(payload.get("merged_refs") or [])
            db.execute("DELETE FROM import_candidates WHERE id = ?", (row["id"],))
            merged_away += 1

        if len(labor_items) > 1:
            # Two crew members only ever surface as separate candidates
            # (one via an "activity", one as a plain record — see
            # _yclients_group_key), so each was built independently by
            # build_import_candidates: the one with no service on its own
            # Yclients record has an empty вид рейса/ставка, and the other
            # may have had the *solo* combined guide+captain rate applied
            # before we knew there was a second crew member. Now that
            # they're merged into one real multi-person trip, reconcile
            # both onto the same вид рейса and the normal (non-solo) rate.
            ref_title = next((i.get("work_type") for i in labor_items if i.get("work_type")), "")
            ref_wt = next((w for w in WORK_TYPES if w["name"] == ref_title), None) if ref_title else None
            for item in labor_items:
                if ref_title:
                    item["work_type"] = ref_title
                if ref_wt:
                    item["rate"] = ref_wt["rate"]
                    if not item.get("quantity"):
                        item["quantity"] = ref_wt["hours"]

        keep_payload["labor_items"] = labor_items or [
            {"employee": "", "work_type": "", "quantity": "", "rate": ""}
        ]
        keep_payload["note"] = " / ".join(n for n in notes if n)
        keep_payload["merged_refs"] = merged_refs
        # items[0] (picked by revenue above) may have been the boat-less
        # partner if its "revenue" happened to sort first — that candidate's
        # boat/fuel_cost/mooring_cost/commission_pct were all left blank
        # when it was built solo (no boat to look them up from), so force
        # the slot's own (always-resolved) boat and recompute the
        # boat-derived numbers fresh, the same way build_import_candidates
        # would have if it had seen both records as one group from the start.
        boat_name = slot[0]
        keep_payload["boat"] = boat_name
        boat_info = boat_lookup(boat_name)
        if boat_info:
            keep_payload["fuel_cost"] = boat_info["fuel"]
            keep_payload["mooring_cost"] = boat_info["mooring"]
            keep_payload["commission_pct"] = (
                boat_info["commission_aggregator"]
                if keep_payload.get("sale_channel") == "aggregator"
                else boat_info["commission_direct"]
            )

        employees_label = ", ".join(
            i["employee"] for i in keep_payload["labor_items"] if i.get("employee")
        ) or "—"
        boat, trip_date, trip_time = slot
        trip_date_label = format_ru_date(trip_date)
        when_label = f"{trip_date_label} {trip_time}".strip() if trip_time else trip_date_label
        revenue = keep_payload.get("revenue") or 0
        summary = f"{when_label} · {boat} · {employees_label} · {format_money(revenue)} ₽"

        db.execute(
            "UPDATE import_candidates SET summary = ?, payload = ? WHERE id = ?",
            (summary, json.dumps(keep_payload, ensure_ascii=False), keep_row["id"]),
        )

    db.commit()
    return merged_away


def _imported_trip_for_candidate_slot(db, payload):
    """Find one already-imported physical trip in the candidate's slot.

    YCLIENTS can represent one crew as an activity record and another crew
    member as a plain record with a different reference. If one reference was
    imported before the other appeared, the late record must join the
    confirmed trip rather than remain an invalid zero-revenue candidate.
    """
    boat = payload.get("boat") or ""
    trip_date = payload.get("trip_date") or ""
    trip_time = payload.get("trip_time") or "00:00"
    if not boat or not trip_date:
        return None
    rows = db.execute(
        "SELECT DISTINCT trips.id FROM trips "
        "JOIN yclients_imports ON yclients_imports.trip_id = trips.id "
        "WHERE trips.boat = ? AND trips.trip_date = ? "
        "AND COALESCE(trips.trip_time, '00:00') = ?",
        (boat, trip_date, trip_time),
    ).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def _known_paid_ref_moved_out_of_merged_trip(db, trip_id, payload):
    """Whether a paid YCLIENTS ref was historically merged into another boat.

    Earlier versions matched any boat-less candidate to the sole resolved
    trip at the same date/time.  Once YCLIENTS returns the missing activity
    colour, a paid ref can therefore reveal that it belongs to a different
    boat.  Only split trips backed by multiple refs: a single-ref booking
    changing boats is a different update case and must not create a duplicate.
    """
    boat = str(payload.get("boat") or "").strip()
    trip_date = str(payload.get("trip_date") or "").strip()
    trip_time = str(payload.get("trip_time") or "00:00").strip()
    if not boat or not trip_date or not (payload.get("revenue") or 0):
        return False

    trip = db.execute(
        "SELECT boat, trip_date, COALESCE(trip_time, '00:00') AS trip_time "
        "FROM trips WHERE id = ?",
        (trip_id,),
    ).fetchone()
    if trip is None:
        return False
    # This repair is intentionally limited to the reported cross-boat
    # collision at one exact departure time.  A legitimate booking whose
    # date/time was edited in YCLIENTS follows the existing update path and
    # must not be split away from its crew placeholder.
    if trip["boat"] == boat or (
        trip["trip_date"], trip["trip_time"]
    ) != (trip_date, trip_time):
        return False

    ref_count = db.execute(
        "SELECT COUNT(*) AS count FROM yclients_imports "
        "WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()["count"]
    return ref_count > 1


def _trip_labor_payload(db, trip_id):
    """Return the currently linked labor rows in import-payload form."""
    trip = db.execute("SELECT entry_id FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        return []
    rows = db.execute(
        "SELECT entries.id, entries.employee, entries.work_type, "
        "entries.quantity, entries.rate FROM trip_labor "
        "JOIN entries ON entries.id = trip_labor.entry_id "
        "WHERE trip_labor.trip_id = ? ORDER BY trip_labor.id",
        (trip_id,),
    ).fetchall()
    seen_entry_ids = {row["id"] for row in rows}
    if trip["entry_id"] and trip["entry_id"] not in seen_entry_ids:
        legacy = db.execute(
            "SELECT id, employee, work_type, quantity, rate FROM entries WHERE id = ?",
            (trip["entry_id"],),
        ).fetchone()
        if legacy is not None:
            rows = [*rows, legacy]
    return [
        {
            "employee": row["employee"],
            "work_type": row["work_type"],
            "quantity": row["quantity"],
            "rate": row["rate"],
        }
        for row in rows
    ]


def _combined_imported_trip_payload(db, trip_id, associated_candidates):
    """Build one authoritative payload from every current YCLIENTS ref.

    Financial fields may only come from a reference that is already linked
    to this trip and still carries a real sale. A late zero-revenue crew
    placeholder is useful for updating payroll, but must never zero out the
    revenue of the previously imported booking.
    """
    has_known_ref = any(is_known_ref for _candidate, is_known_ref in associated_candidates)
    labor_items = [] if has_known_ref else _trip_labor_payload(db, trip_id)
    known_sales = [
        candidate
        for candidate, is_known_ref in associated_candidates
        if is_known_ref and (candidate["payload"].get("revenue") or 0) > 0
    ]
    financial_source = max(
        known_sales,
        key=lambda candidate: candidate["payload"].get("revenue") or 0,
        default=None,
    )
    base_candidate = financial_source or max(
        (candidate for candidate, _is_known_ref in associated_candidates),
        key=lambda candidate: candidate["payload"].get("revenue") or 0,
    )
    base_payload = dict(base_candidate["payload"])
    base_payload["_sync_financials"] = financial_source is not None

    for candidate, _is_known_ref in associated_candidates:
        labor_items.extend(candidate["payload"].get("labor_items") or [])

    deduplicated = []
    seen_employees = set()
    for item in labor_items:
        employee = str(item.get("employee") or "").strip()
        if not employee or employee in seen_employees:
            continue
        seen_employees.add(employee)
        deduplicated.append(dict(item))
    labor_items = deduplicated

    if len(labor_items) > 1:
        ref_title = next(
            (str(item.get("work_type") or "").strip() for item in labor_items if item.get("work_type")),
            "",
        )
        if not ref_title:
            existing = _trip_labor_payload(db, trip_id)
            ref_title = next(
                (str(item.get("work_type") or "").strip() for item in existing if item.get("work_type")),
                "",
            )
        ref_work_type = next(
            (work_type for work_type in WORK_TYPES if work_type["name"] == ref_title),
            None,
        )
        for item in labor_items:
            if ref_title:
                item["work_type"] = ref_title
            if ref_work_type:
                item["rate"] = ref_work_type["rate"]
                if not _is_number(item.get("quantity")):
                    item["quantity"] = ref_work_type["hours"]

    base_payload["labor_items"] = labor_items
    return base_payload


def _cancelled_yclients_groups(records, activity_colors):
    groups = {}
    for record in records:
        if record.get("deleted"):
            continue
        group_key = _yclients_group_key(record)
        groups.setdefault(group_key, []).append(record)
    return {
        group_key: grouped_records
        for group_key, grouped_records in groups.items()
        if _yclients_group_is_cancelled(
            group_key, grouped_records, activity_colors
        )
    }


def _imported_trip_for_cancelled_group(db, group_key, records):
    """Resolve a previously imported trip that is now red in YCLIENTS.

    Activity ids are stable, so group trips resolve directly through
    ``yclients_imports``. A plain booking's reference contains its colour;
    once it turns red that reference changes, so use an intentionally strict
    fallback: same date/time, a service-bearing record and exactly one
    imported trip linked to one of the same employees.
    """
    imported = db.execute(
        "SELECT trip_id FROM yclients_imports "
        "WHERE yclients_ref = ? AND trip_id IS NOT NULL",
        (group_key,),
    ).fetchall()
    trip_ids = {row["trip_id"] for row in imported}
    if len(trip_ids) == 1:
        return next(iter(trip_ids))
    if trip_ids or group_key.startswith("activity:"):
        return None
    if not any(record.get("services") for record in records):
        return None  # a staff blocker, not a cancelled client booking

    trip_date = _yclients_record_date(records[0])
    trip_time = _yclients_record_time(records[0]) or "00:00"
    employees = sorted({
        str((record.get("staff") or {}).get("name") or "").strip()
        for record in records
        if str((record.get("staff") or {}).get("name") or "").strip()
    })
    if not employees:
        return None
    placeholders = ",".join("?" for _employee in employees)
    matches = db.execute(
        "SELECT DISTINCT trips.id FROM trips "
        "JOIN yclients_imports ON yclients_imports.trip_id = trips.id "
        "JOIN trip_labor ON trip_labor.trip_id = trips.id "
        "JOIN entries ON entries.id = trip_labor.entry_id "
        "WHERE trips.trip_date = ? "
        "AND COALESCE(trips.trip_time, '00:00') = ? "
        f"AND entries.employee IN ({placeholders})",
        (trip_date, trip_time, *employees),
    ).fetchall()
    return matches[0]["id"] if len(matches) == 1 else None


def _remove_imported_trip_ids(db, trip_ids, extra_source_refs=()):
    """Remove imported trips and release all external-source references."""
    removed = 0
    fuel_source_refs = set(extra_source_refs)
    for trip_id in trip_ids:
        source_refs = [
            row["yclients_ref"]
            for row in db.execute(
                "SELECT yclients_ref FROM yclients_imports WHERE trip_id = ?",
                (trip_id,),
            ).fetchall()
        ]
        fuel_source_refs.update(source_refs)
        for source_ref in source_refs:
            db.execute(
                "DELETE FROM import_candidates WHERE yclients_ref IN (?, ?)",
                (source_ref, f"recheck:{trip_id}:{source_ref}"),
            )
        if _delete_trip_data(db, trip_id, release_yclients_refs=True):
            removed += 1
    fuel_services.remove_cancelled_yclients_trips(db, fuel_source_refs)
    return removed


def _remove_cancelled_imported_trips(db, records, activity_colors):
    """Remove trips that were marked red after an earlier import."""
    trip_ids = set()
    cancelled_groups = _cancelled_yclients_groups(records, activity_colors)
    for group_key, grouped_records in cancelled_groups.items():
        db.execute(
            "DELETE FROM import_candidates WHERE yclients_ref = ?",
            (group_key,),
        )
        trip_id = _imported_trip_for_cancelled_group(
            db, group_key, grouped_records
        )
        if trip_id is not None:
            trip_ids.add(trip_id)
    return _remove_imported_trip_ids(
        db, trip_ids, extra_source_refs=cancelled_groups
    )


def _remove_missing_imported_trips(db, records, start_date, end_date):
    """Remove imported trips whose YCLIENTS sources disappeared entirely.

    The records endpoint is paginated to completion, so within the requested
    date range its source refs form an authoritative snapshot. A trip may be
    backed by several refs (for example an activity plus a guide placeholder)
    and is removed only when none of those refs remains in the snapshot.
    """
    present_refs = {
        _yclients_group_key(record)
        for record in records
        if not record.get("deleted")
    }
    imported_rows = db.execute(
        "SELECT trips.id AS trip_id, yclients_imports.yclients_ref "
        "FROM trips JOIN yclients_imports ON yclients_imports.trip_id = trips.id "
        "WHERE trips.trip_date BETWEEN ? AND ?",
        (start_date, end_date),
    ).fetchall()
    refs_by_trip = {}
    for row in imported_rows:
        refs_by_trip.setdefault(row["trip_id"], set()).add(row["yclients_ref"])
    missing_trip_ids = {
        trip_id
        for trip_id, source_refs in refs_by_trip.items()
        if source_refs.isdisjoint(present_refs)
    }
    return _remove_imported_trip_ids(db, missing_trip_ids)


def _import_yclients_trip_records(
    db,
    records,
    activity_colors,
    start_date,
    end_date,
    *,
    now=None,
    prune_stale=False,
    scheduled_staff_days=None,
    checked_schedule_employees=None,
    schedule_start_date=None,
    schedule_end_date=None,
    minimum_shift_records=None,
    reconciliation_records=None,
    reconcile_missing=False,
):
    """Import one already-fetched YCLIENTS window, safely on repeated runs.

    The manual form and the hourly cron intentionally share this path so
    both create identical trips, payroll entries and investor calculations.
    Invalid or ambiguous records remain in ``import_candidates`` for an
    administrator instead of being discarded by the background job.
    """
    activity_colors = activity_colors or {}
    current_records = (
        records if reconciliation_records is None else reconciliation_records
    )
    cancelled = _remove_cancelled_imported_trips(
        db,
        current_records,
        activity_colors,
    )
    deleted = (
        _remove_missing_imported_trips(
            db, current_records, start_date, end_date
        )
        if reconcile_missing
        else 0
    )
    already = {
        row["yclients_ref"]: row["trip_id"]
        for row in db.execute("SELECT yclients_ref, trip_id FROM yclients_imports").fetchall()
    }
    existing_candidates = {
        row["yclients_ref"]
        for row in db.execute("SELECT yclients_ref FROM import_candidates").fetchall()
    }
    candidates = build_import_candidates(records, activity_colors)

    if prune_stale:
        # A corrected color/time changes the generated ref. During an
        # explicit period refresh, remove an old queued version that no
        # longer exists in YCLIENTS. The hourly job deliberately does not
        # prune: its completed-only input excludes today's future records.
        fetched_refs = {candidate["yclients_ref"] for candidate in candidates}
        for row in db.execute(
            "SELECT id, yclients_ref, payload FROM import_candidates"
        ).fetchall():
            if row["yclients_ref"] in fetched_refs:
                continue
            trip_date = json.loads(row["payload"]).get("trip_date", "")
            if start_date <= trip_date <= end_date:
                db.execute("DELETE FROM import_candidates WHERE id = ?", (row["id"],))
        db.commit()

    created_at = (now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    added = 0
    payroll_updated = 0
    associated_by_trip = {}
    unassociated = []
    for candidate in candidates:
        ref = candidate["yclients_ref"]
        if ref in already:
            trip_id = already[ref]
            if trip_id is None:
                # Older versions offered the zero-revenue secondary crew
                # record as an invalid standalone card, so an administrator
                # may reasonably have skipped it. If it now matches exactly
                # one imported physical trip, heal that old tombstone and
                # attach the employee; never revive a skipped paying record.
                if not (candidate["payload"].get("revenue") or 0):
                    trip_id = _imported_trip_for_candidate_slot(
                        db, candidate["payload"]
                    )
                    if trip_id is not None:
                        db.execute(
                            "UPDATE yclients_imports SET trip_id = ? "
                            "WHERE yclients_ref = ? AND trip_id IS NULL",
                            (trip_id, ref),
                        )
                        already[ref] = trip_id
                if trip_id is None:
                    continue  # an intentional skip unrelated to a known trip
            if _known_paid_ref_moved_out_of_merged_trip(
                db, trip_id, candidate["payload"]
            ):
                # The ref was joined to this trip only because its own boat
                # colour was unavailable on an earlier import.  Release it;
                # the normal association/import path below will either join
                # the now-resolved boat's existing trip or create that trip.
                db.execute(
                    "DELETE FROM yclients_imports "
                    "WHERE yclients_ref = ? AND trip_id = ?",
                    (ref, trip_id),
                )
                already.pop(ref, None)
                resolved_trip_id = _imported_trip_for_candidate_slot(
                    db, candidate["payload"]
                )
                if resolved_trip_id is not None:
                    associated_by_trip.setdefault(
                        resolved_trip_id, []
                    ).append((candidate, False))
                else:
                    unassociated.append(candidate)
                continue
            associated_by_trip.setdefault(trip_id, []).append((candidate, True))
            continue
        trip_id = _imported_trip_for_candidate_slot(db, candidate["payload"])
        if trip_id is not None:
            associated_by_trip.setdefault(trip_id, []).append((candidate, False))
            continue
        unassociated.append(candidate)

    for trip_id, associated_candidates in associated_by_trip.items():
        combined_payload = _combined_imported_trip_payload(
            db, trip_id, associated_candidates
        )
        if _sync_imported_trip_labor(db, trip_id, combined_payload):
            payroll_updated += 1
        for candidate, is_known_ref in associated_candidates:
            refs = [
                candidate["yclients_ref"],
                *candidate["payload"].get("merged_refs", []),
            ]
            if not is_known_ref:
                _mark_yclients_refs_imported(db, refs, trip_id)
            for source_ref in refs:
                db.execute(
                    "DELETE FROM import_candidates WHERE yclients_ref = ?",
                    (source_ref,),
                )
                existing_candidates.discard(source_ref)
                review_ref = f"recheck:{trip_id}:{source_ref}"
                db.execute(
                    "DELETE FROM import_candidates WHERE yclients_ref = ?",
                    (review_ref,),
                )
                existing_candidates.discard(review_ref)

    for candidate in unassociated:
        ref = candidate["yclients_ref"]
        if ref in existing_candidates:
            # Refresh a queued candidate from the latest YCLIENTS data.
            # Activity colours can be temporarily unavailable; keeping the
            # original boat-less payload forever would force an unnecessary
            # manual correction even after the next fetch resolves the boat.
            db.execute(
                "UPDATE import_candidates SET summary = ?, payload = ? "
                "WHERE yclients_ref = ?",
                (
                    candidate["summary"],
                    json.dumps(candidate["payload"], ensure_ascii=False),
                    ref,
                ),
            )
            continue
        db.execute(
            "INSERT INTO import_candidates (yclients_ref, summary, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                ref,
                candidate["summary"],
                json.dumps(candidate["payload"], ensure_ascii=False),
                created_at,
            ),
        )
        existing_candidates.add(ref)
        added += 1
    db.commit()

    merged = merge_pending_candidates(db)
    imported = 0
    pending = db.execute("SELECT * FROM import_candidates ORDER BY id ASC").fetchall()
    for row in pending:
        if _try_auto_import_candidate(db, row):
            imported += 1

    # Run only after trips have landed in entries, otherwise a shift can get
    # a temporary top-up before its actual trip pay is taken into account.
    topups_changed = apply_minimum_shift_rate(
        db,
        records if minimum_shift_records is None else minimum_shift_records,
        scheduled_staff_days=scheduled_staff_days,
        checked_schedule_employees=checked_schedule_employees,
        schedule_start_date=schedule_start_date,
        schedule_end_date=schedule_end_date,
    )
    pending_total = db.execute(
        "SELECT COUNT(*) AS count FROM import_candidates"
    ).fetchone()["count"]
    return {
        "fetched": len(records),
        "candidates": len(candidates),
        "cancelled": cancelled,
        "deleted": deleted,
        "added": added,
        "merged": merged,
        "imported": imported,
        "payroll_updated": payroll_updated,
        "pending": pending_total,
        "topups_changed": topups_changed,
    }


@app.route("/trips/import", methods=["GET"])
@admin_login_required
def import_index():
    """The import queue lives inside the trips page itself (as a collapsible
    section) — this route just renders that same page with the section
    expanded, so links/redirects built around "go to the import screen"
    still land somewhere sensible."""
    db = get_db()
    ctx = _trips_list_context(db)
    return render_template(
        "trips.html", **ctx, **_trips_common_kwargs(db),
        edit_trip=None,
        import_error=request.args.get("error"),
        import_warning=request.args.get("warning"),
        open_import=True,
    )


@app.route("/trips/import/fetch", methods=["POST"])
@admin_login_required
def import_fetch():
    db = get_db()
    if not yclients_configured():
        return redirect(url_for(
            "import_index",
            error="Не настроены переменные окружения YCLIENTS_PARTNER_TOKEN / "
                  "YCLIENTS_USER_TOKEN / YCLIENTS_COMPANY_ID.",
        ))

    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    try:
        dt.date.fromisoformat(start_date)
        dt.date.fromisoformat(end_date)
    except ValueError:
        return redirect(url_for("import_index", error="Некорректный период."))

    try:
        records = yclients_get_records(start_date, end_date)
    except requests.RequestException as e:
        return redirect(url_for("import_index", error=f"Ошибка соединения с Yclients: {e}"))
    except (RuntimeError, ValueError) as e:
        return redirect(url_for("import_index", error=str(e)))

    activity_ids = {r["activity_id"] for r in records if r.get("activity_id")}
    try:
        activity_colors = yclients_get_activity_colors(activity_ids)
    except requests.RequestException as e:
        return redirect(url_for("import_index", error=f"Ошибка при запросе групповых событий: {e}"))

    payroll_schedule = {
        "staffed_days": set(),
        "checked_employees": set(),
        "failed_employees": [],
        "start_date": None,
        "end_date": None,
    }
    schedule_warning = None
    try:
        payroll_schedule = _yclients_payroll_schedule(db, start_date, end_date)
        if payroll_schedule.get("failed_employees"):
            schedule_warning = (
                "Не удалось проверить: "
                + ", ".join(payroll_schedule["failed_employees"])
                + ". Их существующие доплаты не изменены; остальные сотрудники пересчитаны."
            )
    except (requests.RequestException, RuntimeError, ValueError) as error:
        # A schedule outage must not block trip/revenue import. Existing
        # top-ups are left untouched until a later successful check.
        schedule_warning = (
            "График смен YCLIENTS временно недоступен. Рейсы загружены, "
            "существующие доплаты не изменены."
        )
        print(f"YCLIENTS payroll schedule sync failed: {error}", file=sys.stderr, flush=True)

    _import_yclients_trip_records(
        db,
        records,
        activity_colors,
        start_date,
        end_date,
        prune_stale=True,
        scheduled_staff_days=payroll_schedule["staffed_days"],
        checked_schedule_employees=payroll_schedule["checked_employees"],
        schedule_start_date=payroll_schedule["start_date"],
        schedule_end_date=payroll_schedule["end_date"],
        minimum_shift_records=records,
        reconciliation_records=records,
        reconcile_missing=True,
    )

    if schedule_warning:
        return redirect(url_for("import_index", warning=schedule_warning))
    return redirect(url_for("import_index"))


@app.route("/trips/import/review/<int:candidate_id>", methods=["GET"])
@admin_login_required
def import_review(candidate_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM import_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        return redirect(url_for("import_index"))
    payload = json.loads(row["payload"])

    form_values = {
        "boat": payload["boat"],
        "trip_date": payload["trip_date"],
        "trip_time": payload.get("trip_time") or "00:00",
        "revenue": payload["revenue"],
        "sale_channel": payload["sale_channel"],
        "commission_pct": payload["commission_pct"],
        "fuel_cost": payload["fuel_cost"],
        "mooring_cost": payload["mooring_cost"],
    }
    ctx = _trips_list_context(db)
    return render_template(
        "trips.html", **ctx, **_trips_common_kwargs(db),
        edit_trip=None, import_candidate=row, form_values=form_values,
        labor_prefill=payload["labor_items"],
        import_note=payload.get("note", ""),
    )


def _sync_imported_trip_labor(db, trip_id, payload):
    """Make an imported trip match the latest mutable YCLIENTS fields.

    A booking can be reassigned, shortened or moved to another service after
    its first import. The YCLIENTS reference still points to the same trip,
    so update its crew, work type, duration and current sale amount instead
    of creating a duplicate. Manual trips have no YCLIENTS reference and
    never pass through this function. Returns whether any stored value
    changed.
    """
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is None:
        return False

    labor_items = []
    for item in payload.get("labor_items") or []:
        employee = str(item.get("employee") or "").strip()
        work_type = str(item.get("work_type") or "").strip()
        try:
            quantity = float(item.get("quantity"))
            rate = float(item.get("rate"))
        except (TypeError, ValueError):
            return False
        if not employee or not work_type or quantity < 0 or rate < 0:
            return False
        labor_items.append({
            "employee": employee,
            "work_type": work_type,
            "quantity": quantity,
            "rate": rate,
            "amount": quantity * rate,
        })
    if not labor_items:
        return False

    linked_rows = db.execute(
        "SELECT entries.id, entries.employee, entries.work_type, "
        "entries.quantity, entries.rate FROM trip_labor "
        "JOIN entries ON entries.id = trip_labor.entry_id "
        "WHERE trip_labor.trip_id = ? ORDER BY trip_labor.id",
        (trip_id,),
    ).fetchall()
    old_entry_ids = {row["id"] for row in linked_rows}
    if trip["entry_id"] and trip["entry_id"] not in old_entry_ids:
        legacy = db.execute(
            "SELECT id, employee, work_type, quantity, rate FROM entries WHERE id = ?",
            (trip["entry_id"],),
        ).fetchone()
        if legacy is not None:
            linked_rows = [*linked_rows, legacy]
            old_entry_ids.add(legacy["id"])

    old_labor_signature = sorted(
        (
            row["employee"],
            row["work_type"],
            round(float(row["quantity"]), 6),
            round(float(row["rate"]), 6),
        )
        for row in linked_rows
    )
    new_labor_signature = sorted(
        (
            item["employee"],
            item["work_type"],
            round(item["quantity"], 6),
            round(item["rate"], 6),
        )
        for item in labor_items
    )
    labor_changed = old_labor_signature != new_labor_signature

    revenue = float(trip["revenue"])
    sale_channel = trip["sale_channel"]
    commission_pct = float(trip["commission_pct"])
    if payload.get("_sync_financials"):
        try:
            revenue = float(payload.get("revenue"))
            commission_pct = float(payload.get("commission_pct"))
        except (TypeError, ValueError):
            return False
        candidate_channel = str(payload.get("sale_channel") or "").strip()
        if candidate_channel in {channel["value"] for channel in SALE_CHANNELS}:
            sale_channel = candidate_channel

    commission_amount = revenue * commission_pct / 100
    financial_changed = any((
        abs(float(trip["revenue"]) - revenue) > 0.01,
        trip["sale_channel"] != sale_channel,
        abs(float(trip["commission_pct"]) - commission_pct) > 0.01,
        abs(float(trip["commission_amount"]) - commission_amount) > 0.01,
    ))
    if not labor_changed and not financial_changed:
        return False

    old_staff = {row["employee"] for row in linked_rows}
    new_staff = {item["employee"] for item in labor_items}

    if labor_changed:
        db.execute("DELETE FROM trip_labor WHERE trip_id = ?", (trip_id,))
        for entry_id in old_entry_ids:
            db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))

        # A removed employee may still have a top-up produced by an earlier
        # import of this shift. Drop it now; apply_minimum_shift_rate() below
        # recreates the correct amount if that person still works another
        # YCLIENTS trip on the same date.
        for employee in old_staff - new_staff:
            db.execute(
                "DELETE FROM entries WHERE employee = ? AND work_date = ? AND work_type = ?",
                (employee, trip["trip_date"], MIN_SHIFT_TOPUP_WORK_TYPE),
            )

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry_ids = []
        for item in labor_items:
            cur = db.execute(
                "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item["employee"], item["work_type"], item["rate"],
                    item["quantity"], item["amount"], trip["trip_date"], now,
                ),
            )
            entry_ids.append(cur.lastrowid)
            db.execute(
                "INSERT INTO trip_labor (trip_id, entry_id) VALUES (?, ?)",
                (trip_id, cur.lastrowid),
            )
    else:
        entry_ids = [row["id"] for row in linked_rows]

    labor_cost = sum(item["amount"] for item in labor_items)
    remainder = (
        revenue - commission_amount - labor_cost
        - trip["fuel_cost"] - trip["mooring_cost"] - trip["extra_total"]
    )
    work_types = list(dict.fromkeys(item["work_type"] for item in labor_items))
    db.execute(
        "UPDATE trips SET work_type = ?, entry_id = ?, revenue = ?, sale_channel = ?, "
        "commission_pct = ?, commission_amount = ?, labor_cost = ?, remainder = ?, "
        "investor_payout = ?, my_share = ? WHERE id = ?",
        (
            " + ".join(work_types), entry_ids[0], revenue, sale_channel,
            commission_pct, commission_amount, labor_cost, remainder,
            remainder / 2, commission_amount + remainder / 2, trip_id,
        ),
    )
    return True


def _mark_yclients_refs_imported(db, refs, trip_id):
    """Record every yclients_ref that fed into this trip — not just the one
    candidate row that survived merging — as done. Candidates that get
    merged into another one are deleted outright; if their own ref isn't
    also marked here, the next fetch sees that same raw Yclients record as
    brand new (its merge partner already became a real trip and won't be
    recreated) and re-adds it as an orphaned, partner-less candidate."""
    for ref in refs:
        db.execute(
            "INSERT OR IGNORE INTO yclients_imports (yclients_ref, trip_id) VALUES (?, ?)",
            (ref, trip_id),
        )


def _insert_trip(db, data):
    """Write a validated trip (as returned by _process_trip_form) plus its
    labor entries and extra expenses. Returns the new trip id."""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    entry_ids = []
    for item in data["labor_items"]:
        cur = db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item["employee"], item["work_type"], item["rate"], item["quantity"],
             item["amount"], data["trip_date"], now),
        )
        entry_ids.append(cur.lastrowid)

    cur2 = db.execute(
        "INSERT INTO trips (boat, trip_date, trip_time, work_type, entry_id, revenue, sale_channel, "
        "commission_pct, commission_amount, labor_cost, fuel_cost, mooring_cost, extra_total, "
        "remainder, investor_payout, my_share, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (data["boat"], data["trip_date"], data["trip_time"], data["work_type"],
         entry_ids[0] if entry_ids else None,
         data["revenue"], data["sale_channel"], data["commission_pct"], data["commission_amount"],
         data["labor_cost"], data["fuel_cost"], data["mooring_cost"], data["extra_total"],
         data["remainder"], data["investor_payout"], data["my_share"], now),
    )
    trip_id = cur2.lastrowid
    for eid in entry_ids:
        db.execute("INSERT INTO trip_labor (trip_id, entry_id) VALUES (?, ?)", (trip_id, eid))
    for desc, amt in data["expenses"]:
        db.execute(
            "INSERT INTO trip_expenses (trip_id, description, amount) VALUES (?, ?, ?)",
            (trip_id, desc, amt),
        )
    return trip_id


def _payload_to_form(payload):
    """Turn a stored import-candidate payload back into a form-like
    MultiDict so it can go through the same _process_trip_form validation
    that the manual "Добавить рейс" form uses."""
    form = MultiDict()
    form["boat"] = payload.get("boat", "")
    form["trip_date"] = payload.get("trip_date", "")
    form["trip_time"] = payload.get("trip_time") or "00:00"
    form["sale_channel"] = payload.get("sale_channel", "direct")
    form["commission_pct"] = str(payload.get("commission_pct", ""))
    form["revenue"] = str(payload.get("revenue", ""))
    form["fuel_cost"] = str(payload.get("fuel_cost", ""))
    form["mooring_cost"] = str(payload.get("mooring_cost", ""))
    for item in payload.get("labor_items") or []:
        form.add("employee[]", item.get("employee", ""))
        form.add("work_type[]", item.get("work_type", ""))
        form.add("quantity[]", str(item.get("quantity", "")))
        form.add("rate[]", str(item.get("rate", "")))
    return form


def _try_auto_import_candidate(db, row):
    """Attempt to turn one pending import candidate straight into a trip,
    with no human confirmation step. Returns True and removes the candidate
    row on success; returns False and leaves the row in place (for manual
    review via the existing "Ожидают подтверждения" queue) if the payload
    doesn't validate — most commonly an unresolved boat color or a Yclients
    service name that isn't in YCLIENTS_SERVICE_TO_WORK_TYPE yet (missing
    вид рейса means the hours/rate can't be filled in, which fails
    validation same as an empty field would in the manual form). Also
    returns False without touching anything for a needs_review candidate
    (see import_fetch) — auto-creating a trip for one would just produce a
    duplicate of the trip it's actually about."""
    payload = json.loads(row["payload"])
    if payload.get("needs_review"):
        return False
    form = _payload_to_form(payload)
    errors, data = _process_trip_form(db, form)
    if errors:
        base_summary = row["summary"].split(" ⚠ ", 1)[0]
        reason = "; ".join(errors)
        db.execute(
            "UPDATE import_candidates SET summary = ? WHERE id = ?",
            (f"{base_summary} ⚠ {reason}", row["id"]),
        )
        db.commit()
        return False
    trip_id = _insert_trip(db, data)
    _mark_yclients_refs_imported(db, [row["yclients_ref"], *payload.get("merged_refs", [])], trip_id)
    db.execute("DELETE FROM import_candidates WHERE id = ?", (row["id"],))
    db.commit()
    return True


@app.route("/trips/import/confirm/<int:candidate_id>", methods=["POST"])
@admin_login_required
def import_confirm(candidate_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM import_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        return redirect(url_for("import_index"))

    payload = json.loads(row["payload"])
    errors, data = _process_trip_form(db, request.form)
    if errors:
        ctx = _trips_list_context(db)
        return render_template(
            "trips.html", **ctx, **_trips_common_kwargs(db),
            edit_trip=None, import_candidate=row, errors=errors, form_values=request.form,
            import_note=payload.get("note", ""),
        ), 400

    trip_id = _insert_trip(db, data)
    _mark_yclients_refs_imported(db, [row["yclients_ref"], *payload.get("merged_refs", [])], trip_id)
    db.execute("DELETE FROM import_candidates WHERE id = ?", (candidate_id,))
    db.commit()
    return redirect(url_for("import_index"))


@app.route("/trips/import/skip/<int:candidate_id>", methods=["POST"])
@admin_login_required
def import_skip(candidate_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM import_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is not None:
        payload = json.loads(row["payload"])
        _mark_yclients_refs_imported(
            db, [row["yclients_ref"], *payload.get("merged_refs", [])], None
        )
        db.execute("DELETE FROM import_candidates WHERE id = ?", (candidate_id,))
        db.commit()
    return redirect(url_for("import_index"))


# ---------------------------------------------------------------------
# Личный кабинет инвестора
# ---------------------------------------------------------------------

def investor_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("investor_id"):
            return redirect(url_for("investor_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/investor/login", methods=["GET", "POST"])
def investor_login():
    if request.method == "GET":
        if session.get("investor_id"):
            return redirect(url_for("investor_dashboard"))
        return render_template("investor_login.html", error=None)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    db = get_db()
    row = db.execute(
        "SELECT * FROM investors WHERE username = ?", (username,)
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return render_template(
            "investor_login.html", error="Неверный логин или пароль.",
        ), 401

    session.clear()
    session["investor_id"] = row["id"]
    session["investor_name"] = row["investor_name"]
    return redirect(url_for("investor_dashboard"))


@app.route("/investor/logout", methods=["POST"])
def investor_logout():
    session.clear()
    return redirect(url_for("investor_login"))


@app.route("/investor/")
@investor_login_required
def investor_dashboard():
    db = get_db()
    investor_name = session.get("investor_name")
    investor_boats = [b["name"] for b in BOATS if b["investor"] == investor_name]

    months, current_key = build_month_options(db)
    selected_month = request.args.get("month", current_key)

    trip_rows = []
    if investor_boats:
        query = (
            "SELECT * FROM trips WHERE boat IN (%s)"
            % ",".join("?" for _ in investor_boats)
        )
        params = list(investor_boats)
        if selected_month != "all":
            query += " AND substr(trip_date, 1, 7) = ?"
            params.append(selected_month)
        query += " ORDER BY trip_date DESC, id DESC"
        trip_rows = db.execute(query, params).fetchall()

    by_boat, _by_investor, _grand_my_share, grand_revenue = compute_trip_totals(trip_rows)
    grand_payout = sum(b["investor_payout"] for b in by_boat.values())

    return render_template(
        "investor_dashboard.html",
        investor_name=investor_name,
        boats=investor_boats,
        months=months,
        selected_month=selected_month,
        by_boat=by_boat,
        trips=trip_rows,
        grand_revenue=grand_revenue,
        grand_payout=grand_payout,
    )


# ---------------------------------------------------------------------
# Личный кабинет члена команды
# ---------------------------------------------------------------------

def _employee_has_position(db, employee_name, position):
    return db.execute(
        "SELECT 1 FROM employees JOIN employee_positions ON employee_positions.employee_id = employees.id "
        "WHERE employees.name = ? AND employee_positions.position = ? "
        "AND employees.deleted_at IS NULL",
        (employee_name, position),
    ).fetchone() is not None


def _employees_with_any_position(db, positions):
    placeholders = ",".join("?" * len(positions))
    rows = db.execute(
        f"SELECT DISTINCT employees.name FROM employees "
        f"JOIN employee_positions ON employee_positions.employee_id = employees.id "
        f"WHERE employee_positions.position IN ({placeholders}) "
        f"AND employees.deleted_at IS NULL ORDER BY employees.name",
        positions,
    ).fetchall()
    return [r["name"] for r in rows]


def _project_id_for_tuning_order(db, order_id):
    row = db.execute("SELECT id FROM projects WHERE tuning_order_id = ?", (order_id,)).fetchone()
    return row["id"] if row else None


def _resolve_transaction_target(db, raw):
    """Parses the value of a transaction/split "target" <select> — see
    _transactions_table.html and transaction_split.html — into
    (project_id, item_id). The option value is "p<project_id>" for a
    whole-project attribution (item_id stays None, same as before this
    feature existed) or "i<tuning_order_items.id>" for a specific work
    item, in which case the project is resolved from the item's own
    order — the item alone is enough to know which project it belongs to,
    so the <select> only needs to encode one id, not a pair."""
    raw = (raw or "").strip()
    if raw[:1] == "i" and raw[1:].isdigit():
        item_id = int(raw[1:])
        item = db.execute(
            "SELECT order_id FROM tuning_order_items WHERE id = ?", (item_id,)
        ).fetchone()
        if item is None:
            return None, None
        return _project_id_for_tuning_order(db, item["order_id"]), item_id
    if raw[:1] == "p" and raw[1:].isdigit():
        return int(raw[1:]), None
    return None, None


def _items_by_project(db):
    """All work items across every project, keyed by project_id — used to
    build the "p<id>"/"i<id>" <optgroup> options in the transaction
    project/item picker. Fetched as one query rather than per-row/per-split
    to avoid an N+1 query per transaction on the Analytics page."""
    rows = db.execute(
        "SELECT tuning_order_items.*, projects.id AS project_id "
        "FROM tuning_order_items "
        "JOIN tuning_orders ON tuning_orders.id = tuning_order_items.order_id "
        "JOIN projects ON projects.tuning_order_id = tuning_orders.id "
        "ORDER BY tuning_order_items.id"
    ).fetchall()
    by_project = {}
    for r in rows:
        by_project.setdefault(r["project_id"], []).append(r)
    return by_project


def team_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        team_id = session.get("team_id")
        account = None
        if team_id:
            account = get_db().execute(
                "SELECT team_accounts.id FROM team_accounts "
                "JOIN employees ON employees.id = team_accounts.employee_id "
                "WHERE team_accounts.id = ? AND employees.deleted_at IS NULL",
                (team_id,),
            ).fetchone()
        if account is None:
            session.pop("team_id", None)
            session.pop("team_employee_name", None)
            session.pop("team_username", None)
            return redirect(url_for("team_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/team/login", methods=["GET", "POST"])
def team_login():
    if request.method == "GET":
        if session.get("team_id"):
            return redirect(url_for("team_dashboard"))
        return render_template("team_login.html", error=None)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    db = get_db()
    row = db.execute(
        "SELECT team_accounts.* FROM team_accounts "
        "JOIN employees ON employees.id = team_accounts.employee_id "
        "WHERE team_accounts.username = ? AND employees.deleted_at IS NULL",
        (username,),
    ).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return render_template(
            "team_login.html", error="Неверный логин или пароль.",
        ), 401

    session.clear()
    session["team_id"] = row["id"]
    session["team_employee_name"] = row["employee_name"]
    session["team_username"] = row["username"]
    return redirect(url_for("team_dashboard"))


@app.route("/team/logout", methods=["POST"])
def team_logout():
    session.clear()
    return redirect(url_for("team_login"))


@app.route("/team/")
@team_login_required
def team_dashboard():
    db = get_db()
    if _is_customer_manager(db):
        return redirect(url_for("schedule.index"))
    employee_name = session.get("team_employee_name")
    active_section = request.args.get("section", "").strip()

    weeks, current_monday = build_week_options(db)
    selected_week = request.args.get("week", current_monday.isoformat())

    # Same trip_time left-join as the admin Зарплаты page, scoped to just
    # this one employee — they only ever see their own entries.
    query = (
        "SELECT entries.*, trips.trip_time AS trip_time FROM entries "
        "LEFT JOIN trip_labor ON trip_labor.entry_id = entries.id "
        "LEFT JOIN trips ON trips.id = trip_labor.trip_id "
        "WHERE entries.employee = ?"
    )
    params = [employee_name]

    if selected_week != "all":
        try:
            monday = dt.date.fromisoformat(selected_week)
        except ValueError:
            monday = current_monday
            selected_week = monday.isoformat()
        sunday = monday + dt.timedelta(days=6)
        query += " AND entries.work_date BETWEEN ? AND ?"
        params += [monday.isoformat(), sunday.isoformat()]

    query += " ORDER BY entries.work_date DESC, entries.id DESC"
    entries = db.execute(query, params).fetchall()
    total = sum(e["amount"] for e in entries)

    # "Оплачено" only means anything for one concrete week, same as on the
    # admin side — no single period to check it against under "Все периоды".
    is_paid = False
    if selected_week != "all":
        is_paid = db.execute(
            "SELECT 1 FROM payments WHERE employee = ? AND period_key = ?",
            (employee_name, selected_week),
        ).fetchone() is not None

    is_captain = _employee_has_position(db, employee_name, "Капитан")

    # Fleet workspace — one selected boat drives its checklists, documents
    # and defect lists. Only captains see it.
    boat_index = 0
    selected_boat = None
    boat_documents = []
    boat_current_defects = []
    boat_archived_defects = []
    fuel = None
    diploma_url = None
    if is_captain:
        try:
            boat_index = int(request.args.get("boat_index", "0"))
        except ValueError:
            boat_index = 0
        if not (0 <= boat_index < len(BOATS)):
            boat_index = 0
        selected_boat = BOATS[boat_index]["name"]
        boat_documents = db.execute(
            "SELECT * FROM boat_documents WHERE boat = ? ORDER BY uploaded_at DESC, id DESC",
            (selected_boat,),
        ).fetchall()
        boat_defects = db.execute(
            "SELECT * FROM boat_defects WHERE boat = ? ORDER BY updated_at DESC, id DESC",
            (selected_boat,),
        ).fetchall()
        boat_current_defects = [d for d in boat_defects if d["status"] != "resolved"]
        boat_archived_defects = [d for d in boat_defects if d["status"] == "resolved"]
        fuel = fuel_services.fuel_summary(db, selected_boat)
        diploma_url = find_diploma_url(session.get("team_username"))

    # "Мои задачи" — defects and tuning-order work items a captain/tuningman
    # has been handed as paid work. Anyone eligible to be assigned one sees
    # the module, whether or not they currently have any (matches the other
    # modules always showing, just possibly empty).
    can_have_tasks = any(
        _employee_has_position(db, employee_name, p)
        for p in set(DEFECT_ASSIGNABLE_POSITIONS) | set(TUNING_ASSIGNABLE_POSITIONS)
    )
    my_tasks = []
    if can_have_tasks:
        defect_tasks = db.execute(
            "SELECT da.*, bd.boat AS defect_boat, bd.description AS defect_description, "
            "bd.status AS defect_status "
            "FROM defect_assignments da JOIN boat_defects bd ON bd.id = da.defect_id "
            "WHERE da.employee_name = ?",
            (employee_name,),
        ).fetchall()
        tuning_tasks = db.execute(
            "SELECT ta.*, ti.work_name AS item_work_name, ti.status AS item_status, "
            "tord.client_name AS order_client_name, tord.boat_model AS order_boat_model, "
            "tord.boat_registration_number AS order_boat_registration_number, "
            "tord.equipment_type AS order_equipment_type, "
            "tord.motor_model AS order_motor_model, "
            "tord.motor_serial_number AS order_motor_serial_number, "
            "tord.id AS tuning_order_id "
            "FROM tuning_item_assignments ta "
            "JOIN tuning_order_items ti ON ti.id = ta.item_id "
            "JOIN tuning_orders tord ON tord.id = ti.order_id "
            "WHERE ta.employee_name = ?",
            (employee_name,),
        ).fetchall()
        tuning_task_dicts = []
        for t in tuning_tasks:
            task = dict(t, kind="tuning")
            task["equipment_label"] = tuning_equipment_label({
                "equipment_type": task["order_equipment_type"],
                "boat_model": task["order_boat_model"],
                "boat_registration_number": task["order_boat_registration_number"],
                "motor_model": task["order_motor_model"],
                "motor_serial_number": task["order_motor_serial_number"],
            })
            materials = db.execute(
                "SELECT sw.*, sp.name AS product_name "
                "FROM supply_writeoffs sw JOIN supply_products sp ON sp.id = sw.product_id "
                "WHERE sw.tuning_item_assignment_id = ? ORDER BY sw.id DESC",
                (task["id"],),
            ).fetchall()
            task["materials"] = materials
            task["materials_total"] = sum(m["amount"] for m in materials)
            tuning_task_dicts.append(task)
        my_tasks = sorted(
            [dict(t, kind="defect") for t in defect_tasks] + tuning_task_dicts,
            key=lambda t: (t["assigned_at"], t["id"]),
            reverse=True,
        )

    # Materials catalog for the "Списать материалы" modal — every product/
    # warehouse combo that currently has stock, org-wide (not scoped to any
    # one task, since any material could plausibly go toward any job).
    materials = []
    if can_have_tasks:
        materials = db.execute(
            "SELECT sp.id AS product_id, sp.name AS product_name, "
            "sw.id AS warehouse_id, sw.name AS warehouse_name, ss.quantity AS quantity "
            "FROM supply_stock ss "
            "JOIN supply_products sp ON sp.id = ss.product_id "
            "JOIN supply_warehouses sw ON sw.id = ss.warehouse_id "
            "WHERE ss.quantity > 0 ORDER BY sp.name, sw.name"
        ).fetchall()

    # "Заявки на снабжение" mini-section — special supply this employee has
    # asked for that isn't on any shelf. Status/comment changes made by the
    # admin in /supply/requests show up here on next load, since both sides
    # read the same supply_requests row.
    my_supply_requests = []
    if can_have_tasks:
        my_supply_requests = []
        for r in db.execute(
            "SELECT * FROM supply_requests WHERE employee_name = ? ORDER BY created_at DESC, id DESC",
            (employee_name,),
        ).fetchall():
            req = dict(r)
            # Not "items" — dict already has a builtin .items() method, and
            # Jinja's attribute lookup would resolve to that instead of this
            # key, silently hiding the real request lines in the template.
            req["lines"] = db.execute(
                "SELECT * FROM supply_request_items WHERE request_id = ? ORDER BY id",
                (r["id"],),
            ).fetchall()
            my_supply_requests.append(req)

    return render_template(
        "team_dashboard.html",
        employee_name=employee_name,
        weeks=weeks,
        selected_week=selected_week,
        entries=entries,
        total=total,
        is_captain=is_captain,
        is_paid=is_paid,
        avatar_url=find_avatar_url(session.get("team_username")),
        boats=BOATS, boat_index=boat_index, selected_boat=selected_boat,
        boat_documents=boat_documents, boat_current_defects=boat_current_defects,
        boat_archived_defects=boat_archived_defects, fuel=fuel,
        fuel_notice=session.pop("fuel_notice", None), diploma_url=diploma_url,
        defect_notice=session.pop("defect_notice", None),
        income_open=(
            active_section == "income"
            or ("week" in request.args and "boat_index" not in request.args)
        ),
        fleet_open=is_captain and active_section not in {
            "income", "tasks", "supply", "documents",
        },
        documents_open=active_section == "documents",
        tasks_open=active_section == "tasks",
        supply_open=active_section == "supply",
        can_have_tasks=can_have_tasks, my_tasks=my_tasks, defect_statuses=DEFECT_STATUSES,
        work_statuses=WORK_STATUSES, materials=materials,
        team_writeoff_error=session.pop("team_writeoff_error", None),
        my_supply_requests=my_supply_requests, supply_request_statuses=SUPPLY_REQUEST_STATUSES,
    )


def _team_selected_boat(db):
    employee_name = session.get("team_employee_name")
    if not _employee_has_position(db, employee_name, "Капитан"):
        return None, None
    try:
        boat_index = int(request.form.get("boat_index", "0"))
    except ValueError:
        return None, None
    if not (0 <= boat_index < len(BOATS)):
        return None, None
    return boat_index, BOATS[boat_index]["name"]


def _team_dashboard_section_redirect(section):
    """Return to the same cabinet context after an inline dashboard action."""
    params = {"section": section}
    boat_index_raw = request.form.get("boat_index", "").strip()
    if boat_index_raw.isdigit():
        boat_index = int(boat_index_raw)
        if 0 <= boat_index < len(BOATS):
            params["boat_index"] = boat_index
    return redirect(url_for("team_dashboard", **params) + f"#team-{section}")


@app.route("/team/defects", methods=["POST"])
@team_login_required
def team_create_defect():
    db = get_db()
    boat_index, boat = _team_selected_boat(db)
    if boat is None:
        return redirect(url_for("team_dashboard"))

    employee_name = session.get("team_employee_name") or "Капитан"
    description = request.form.get("description", "")
    success, message, defect_id = _create_manual_defect(
        db, boat, description, employee_name
    )
    session["defect_notice"] = {
        "type": "success" if success else "error",
        "message": message,
    }
    if success:
        clean_description = description.strip()
        dispatch_notification(
            db,
            EVENT_FLEET_DEFECT_CREATED,
            f"⚠️ <b>Неисправность добавлена вручную</b> — {html.escape(boat)}\n"
            f"Капитан: {html.escape(employee_name)}\n"
            f"Описание: {html.escape(clean_description)}",
            send_telegram_notification,
        )
        send_push_notification(
            f"Новая неисправность — {boat}",
            clean_description,
            url=f"/fleet/{boat_index}/defects/{defect_id}",
        )
    return redirect(
        url_for("team_dashboard", boat_index=boat_index) + "#captain-defects"
    )


@app.route("/team/fuel/refill", methods=["POST"])
@team_login_required
def team_add_fuel_refill():
    db = get_db()
    boat_index, boat = _team_selected_boat(db)
    if boat is None:
        return redirect(url_for("team_dashboard"))
    operation = request.form.get("fuel_operation", "tank")
    occurred_at = fuel_services.resolve_operation_timestamp(
        request.form.get("occurred_at", ""),
        request.form.get("occurred_at_auto") == "1",
    )
    if operation == "reserve_to_boat":
        success, message = fuel_services.transfer_reserve_between_boats(
            db,
            boat,
            request.form.get("destination_boat", ""),
            request.form.get("liters", ""),
            occurred_at,
            "team",
            session.get("team_employee_name") or "Капитан",
        )
    else:
        success, message = fuel_services.record_refill(
            db,
            boat,
            request.form.get("liters", ""),
            occurred_at,
            request.form.get("fill_to_full") == "1",
            "team",
            session.get("team_employee_name") or "Капитан",
            operation,
        )
    session["fuel_notice"] = {
        "type": "success" if success else "error",
        "message": message,
    }
    return redirect(url_for("team_dashboard", boat_index=boat_index))


@app.route("/team/fuel/trips/<int:event_id>/consumption", methods=["POST"])
@team_login_required
def team_set_manual_fuel_consumption(event_id):
    db = get_db()
    boat_index, boat = _team_selected_boat(db)
    if boat is None:
        return redirect(url_for("team_dashboard"))
    success, message = fuel_services.record_individual_consumption(
        db,
        boat,
        event_id,
        request.form.get("liters", ""),
        "team",
        session.get("team_employee_name") or "Капитан",
    )
    session["fuel_notice"] = {
        "type": "success" if success else "error",
        "message": message,
    }
    return redirect(url_for("team_dashboard", boat_index=boat_index))


def _team_defect_for_employee(db, defect_id, employee_name):
    if _employee_has_position(db, employee_name, "Капитан"):
        return db.execute("SELECT * FROM boat_defects WHERE id = ?", (defect_id,)).fetchone()
    return db.execute(
        "SELECT bd.* FROM boat_defects bd WHERE bd.id = ? AND "
        "(bd.employee_name = ? OR EXISTS ("
        "SELECT 1 FROM defect_assignments da WHERE da.defect_id = bd.id AND da.employee_name = ?"
        "))",
        (defect_id, employee_name, employee_name),
    ).fetchone()


@app.route("/team/defects/<int:defect_id>", methods=["GET", "POST"])
@team_login_required
def team_defect_detail(defect_id):
    db = get_db()
    defect = _team_defect_for_employee(db, defect_id, session.get("team_employee_name"))
    if defect is None:
        return redirect(url_for("team_dashboard"))
    if request.method == "POST":
        _save_defect_case_notes(db, defect_id, request.form)
        return redirect(url_for("team_defect_detail", defect_id=defect_id))
    return render_template(
        "defect_detail.html", **_defect_detail_context(db, defect, "team")
    )


@app.route("/team/defects/<int:defect_id>/plan", methods=["POST"])
@team_login_required
def team_add_defect_plan_item(defect_id):
    db = get_db()
    defect = _team_defect_for_employee(db, defect_id, session.get("team_employee_name"))
    if defect is not None:
        _add_defect_plan_item(db, defect_id, request.form)
    return redirect(url_for("team_defect_detail", defect_id=defect_id))


@app.route("/team/defects/<int:defect_id>/plan/<int:item_id>/status", methods=["POST"])
@team_login_required
def team_set_defect_plan_item_status(defect_id, item_id):
    db = get_db()
    defect = _team_defect_for_employee(db, defect_id, session.get("team_employee_name"))
    if defect is not None:
        _set_defect_plan_item_status(db, defect_id, item_id, request.form.get("status", ""))
    return redirect(url_for("team_defect_detail", defect_id=defect_id))


@app.route("/team/documents/boat/<int:doc_id>")
@team_login_required
def team_download_boat_document(doc_id):
    db = get_db()
    employee_name = session.get("team_employee_name")
    if not _employee_has_position(db, employee_name, "Капитан"):
        return redirect(url_for("team_dashboard"))
    doc = db.execute("SELECT * FROM boat_documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        return redirect(url_for("team_dashboard"))
    docs_dir = os.path.join(app.static_folder, "boat_documents")
    return send_from_directory(
        docs_dir, doc["filename"], download_name=doc["original_filename"],
    )


@app.route("/team/tasks/<int:assignment_id>/respond", methods=["POST"])
@team_login_required
def team_task_respond(assignment_id):
    db = get_db()
    employee_name = session.get("team_employee_name")
    assignment = db.execute(
        "SELECT * FROM defect_assignments WHERE id = ? AND employee_name = ?",
        (assignment_id, employee_name),
    ).fetchone()
    if assignment is None or assignment["assignment_status"] != "pending":
        return _team_dashboard_section_redirect("tasks")
    response = request.form.get("response", "").strip()
    if response in ("accepted", "rejected"):
        db.execute(
            "UPDATE defect_assignments SET assignment_status = ?, responded_at = ? WHERE id = ?",
            (response, dt.datetime.now().strftime("%Y-%m-%d %H:%M"), assignment_id),
        )
        db.commit()
    return _team_dashboard_section_redirect("tasks")


@app.route("/team/tasks/<int:assignment_id>/status", methods=["POST"])
@team_login_required
def team_task_set_status(assignment_id):
    """The captain/tuningman moves their accepted task through the same
    Новая/В работе/На контроле/Устранена statuses the admin sees on the defect itself
    (this writes straight to boat_defects.status — one shared field, so
    there's nothing to keep in sync). Marking it "resolved" for the first
    time also pays out the agreed rate × norm-hours as a real payroll entry,
    guarded by entry_id so toggling the status back and forth can't pay
    twice."""
    db = get_db()
    employee_name = session.get("team_employee_name")
    assignment = db.execute(
        "SELECT * FROM defect_assignments WHERE id = ? AND employee_name = ?",
        (assignment_id, employee_name),
    ).fetchone()
    if assignment is None or assignment["assignment_status"] != "accepted":
        return _team_dashboard_section_redirect("tasks")
    status = request.form.get("status", "").strip()
    if status not in [s["value"] for s in DEFECT_STATUSES]:
        return _team_dashboard_section_redirect("tasks")

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "UPDATE boat_defects SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, assignment["defect_id"]),
    )
    if status == "resolved" and not assignment["entry_id"]:
        amount = assignment["rate"] * assignment["norm_hours"]
        cur = db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (employee_name, DEFECT_TASK_WORK_TYPE, assignment["rate"], assignment["norm_hours"],
             amount, dt.date.today().isoformat(), now),
        )
        db.execute(
            "UPDATE defect_assignments SET entry_id = ? WHERE id = ?",
            (cur.lastrowid, assignment_id),
        )
    db.commit()
    return _team_dashboard_section_redirect("tasks")


@app.route("/team/tuning-tasks/<int:assignment_id>/respond", methods=["POST"])
@team_login_required
def team_tuning_task_respond(assignment_id):
    db = get_db()
    employee_name = session.get("team_employee_name")
    assignment = db.execute(
        "SELECT * FROM tuning_item_assignments WHERE id = ? AND employee_name = ?",
        (assignment_id, employee_name),
    ).fetchone()
    if assignment is None or assignment["assignment_status"] != "pending":
        return _team_dashboard_section_redirect("tasks")
    response = request.form.get("response", "").strip()
    if response in ("accepted", "rejected"):
        db.execute(
            "UPDATE tuning_item_assignments SET assignment_status = ?, responded_at = ? WHERE id = ?",
            (response, dt.datetime.now().strftime("%Y-%m-%d %H:%M"), assignment_id),
        )
        db.commit()
    return _team_dashboard_section_redirect("tasks")


@app.route("/team/tuning-tasks/<int:assignment_id>/status", methods=["POST"])
@team_login_required
def team_tuning_task_set_status(assignment_id):
    """Mirrors team_task_set_status for tuning-order work items: writes
    straight to tuning_order_items.status (the same field the admin's own
    Статусы работ table edits) and pays out rate × norm-hours the first time
    the item reaches "done", guarded by entry_id against double payment."""
    db = get_db()
    employee_name = session.get("team_employee_name")
    assignment = db.execute(
        "SELECT * FROM tuning_item_assignments WHERE id = ? AND employee_name = ?",
        (assignment_id, employee_name),
    ).fetchone()
    if assignment is None or assignment["assignment_status"] != "accepted":
        return _team_dashboard_section_redirect("tasks")
    status = request.form.get("status", "").strip()
    # "Задача снята" takes the work out of the order's total — an admin-only
    # call, not something the assigned employee can set on their own task.
    if status == "removed" or status not in [s["value"] for s in WORK_STATUSES]:
        return _team_dashboard_section_redirect("tasks")

    item = db.execute(
        "SELECT * FROM tuning_order_items WHERE id = ?", (assignment["item_id"],)
    ).fetchone()
    if item is None:
        return _team_dashboard_section_redirect("tasks")

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "UPDATE tuning_order_items SET status = ? WHERE id = ?",
        (status, assignment["item_id"]),
    )
    if status == "done" and not assignment["entry_id"]:
        amount = assignment["rate"] * assignment["norm_hours"]
        project_id = _project_id_for_tuning_order(db, item["order_id"])
        cur = db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at, project_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (employee_name, item["work_name"], assignment["rate"], assignment["norm_hours"],
             amount, dt.date.today().isoformat(), now, project_id),
        )
        db.execute(
            "UPDATE tuning_item_assignments SET entry_id = ? WHERE id = ?",
            (cur.lastrowid, assignment_id),
        )
    db.commit()
    return _team_dashboard_section_redirect("tasks")


@app.route("/team/tuning-tasks/<int:assignment_id>/writeoff-material", methods=["POST"])
@team_login_required
def team_tuning_task_writeoff_material(assignment_id):
    """Lets a tuningman spend catalog materials against their accepted task.
    The cost is snapshotted (quantity × current cost_price) and attributed
    to the task's project, so Analytics reflects it alongside the payroll
    cost of the same work — see _project_id_for_tuning_order."""
    db = get_db()
    employee_name = session.get("team_employee_name")
    assignment = db.execute(
        "SELECT * FROM tuning_item_assignments WHERE id = ? AND employee_name = ?",
        (assignment_id, employee_name),
    ).fetchone()
    if assignment is None or assignment["assignment_status"] != "accepted":
        return _team_dashboard_section_redirect("tasks")
    item = db.execute(
        "SELECT * FROM tuning_order_items WHERE id = ?", (assignment["item_id"],)
    ).fetchone()
    if item is None:
        return _team_dashboard_section_redirect("tasks")

    combo_raw = request.form.get("product_warehouse", "").strip()
    quantity_raw = request.form.get("quantity", "").strip().replace(",", ".")

    errors = []
    product_id = warehouse_id = None
    if ":" in combo_raw:
        pid_raw, wid_raw = combo_raw.split(":", 1)
        if pid_raw.isdigit() and wid_raw.isdigit():
            product_id, warehouse_id = int(pid_raw), int(wid_raw)
    if product_id is None or warehouse_id is None:
        errors.append("Выберите материал.")

    stock_row = product = None
    if product_id is not None and warehouse_id is not None:
        stock_row = db.execute(
            "SELECT * FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
            (product_id, warehouse_id),
        ).fetchone()
        product = db.execute("SELECT * FROM supply_products WHERE id = ?", (product_id,)).fetchone()

    quantity = None
    try:
        quantity = float(quantity_raw)
        if quantity <= 0:
            errors.append("Количество должно быть больше нуля.")
        elif stock_row is None or quantity > stock_row["quantity"]:
            errors.append("Нельзя списать больше, чем есть на складе.")
    except ValueError:
        errors.append("Количество должно быть числом.")

    if errors:
        session["team_writeoff_error"] = " ".join(errors)
        return _team_dashboard_section_redirect("tasks")

    project_id = _project_id_for_tuning_order(db, item["order_id"])
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    amount = quantity * product["cost_price"]
    db.execute(
        "INSERT INTO supply_writeoffs (product_id, warehouse_id, quantity, reason, note, created_at, "
        "project_id, cost_price, amount, employee_name, tuning_item_assignment_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (product_id, warehouse_id, quantity, TUNING_MATERIAL_WRITEOFF_REASON, item["work_name"], now,
         project_id, product["cost_price"], amount, employee_name, assignment_id),
    )
    db.execute(
        "UPDATE supply_stock SET quantity = quantity - ? WHERE id = ?",
        (quantity, stock_row["id"]),
    )
    db.commit()
    _maybe_create_low_stock_request(db, product_id)
    return _team_dashboard_section_redirect("tasks")


@app.route("/team/supply-requests/create", methods=["POST"])
@team_login_required
def team_create_supply_request():
    db = get_db()
    employee_name = session.get("team_employee_name")
    if not any(_employee_has_position(db, employee_name, p) for p in SUPPLY_REQUEST_POSITIONS):
        return _team_dashboard_section_redirect("supply")

    item_names = request.form.getlist("item_name[]")
    quantities = request.form.getlist("quantity[]")

    items = []
    for i in range(max(len(item_names), len(quantities))):
        name = item_names[i].strip() if i < len(item_names) else ""
        qty_raw = quantities[i].strip().replace(",", ".") if i < len(quantities) else ""
        if not name:
            continue
        try:
            qty = float(qty_raw)
        except ValueError:
            continue
        if qty <= 0:
            continue
        items.append((name, qty))

    if not items:
        return _team_dashboard_section_redirect("supply")

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = db.execute(
        "INSERT INTO supply_requests (employee_name, status, created_at) VALUES (?, ?, ?)",
        (employee_name, DEFAULT_SUPPLY_REQUEST_STATUS, now),
    )
    request_id = cur.lastrowid
    for name, qty in items:
        db.execute(
            "INSERT INTO supply_request_items (request_id, item_name, quantity) VALUES (?, ?, ?)",
            (request_id, name, qty),
        )
    db.commit()
    return _team_dashboard_section_redirect("supply")


@app.route("/team/checklist/start/<checklist_type>", methods=["GET", "POST"])
@team_login_required
def team_checklist_start(checklist_type):
    if checklist_type not in CHECKLIST_QUESTIONS:
        return redirect(url_for("team_dashboard"))
    db = get_db()
    employee_name = session.get("team_employee_name")
    if not _employee_has_position(db, employee_name, "Капитан"):
        return redirect(url_for("team_dashboard"))

    if request.method == "GET":
        return render_template(
            "team_checklist_start.html",
            checklist_type=checklist_type,
            checklist_label=CHECKLIST_TYPE_LABELS[checklist_type],
            boats=[b["name"] for b in BOATS],
        )

    boat = request.form.get("boat", "").strip()
    if boat not in [item["name"] for item in BOATS]:
        return render_template(
            "team_checklist_start.html",
            checklist_type=checklist_type,
            checklist_label=CHECKLIST_TYPE_LABELS[checklist_type],
            boats=[b["name"] for b in BOATS],
            error="Выберите катер из списка.",
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = db.execute(
        "INSERT INTO boat_checklists (employee_name, checklist_type, boat, started_at) "
        "VALUES (?, ?, ?, ?)",
        (employee_name, checklist_type, boat, now),
    )
    db.commit()
    return redirect(url_for("team_checklist_run", checklist_id=cur.lastrowid))


@app.route("/team/checklist/<int:checklist_id>")
@team_login_required
def team_checklist_run(checklist_id):
    db = get_db()
    employee_name = session.get("team_employee_name")
    checklist = db.execute(
        "SELECT * FROM boat_checklists WHERE id = ? AND employee_name = ?",
        (checklist_id, employee_name),
    ).fetchone()
    if checklist is None:
        return redirect(url_for("team_dashboard"))

    questions = _checklist_questions_for(checklist["checklist_type"], checklist["boat"])
    answers = db.execute(
        "SELECT * FROM boat_checklist_answers WHERE checklist_id = ? ORDER BY question_index",
        (checklist_id,),
    ).fetchall()
    current_index = len(answers)

    if current_index >= len(questions):
        if not checklist["completed_at"]:
            db.execute(
                "UPDATE boat_checklists SET completed_at = ? WHERE id = ?",
                (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), checklist_id),
            )
            db.commit()
        problems = [
            {"question_text": a["question_text"], "comment": a["comment"],
             "photos": get_checklist_answer_photos(db, a["id"])}
            for a in answers if a["status"] == "problem"
        ]
        extra_defects = db.execute(
            "SELECT * FROM boat_defects WHERE checklist_id = ? AND answer_id IS NULL ORDER BY id",
            (checklist_id,),
        ).fetchall()
        return render_template(
            "team_checklist_run.html", checklist=checklist,
            checklist_label=CHECKLIST_TYPE_LABELS.get(checklist["checklist_type"], ""),
            done=True, problems=problems, total=len(questions), extra_defects=extra_defects,
        )

    return render_template(
        "team_checklist_run.html", checklist=checklist,
        checklist_label=CHECKLIST_TYPE_LABELS.get(checklist["checklist_type"], ""),
        done=False, question=questions[current_index],
        question_index=current_index, total=len(questions),
    )


@app.route("/team/checklist/<int:checklist_id>/answer", methods=["POST"])
@team_login_required
def team_checklist_answer(checklist_id):
    db = get_db()
    employee_name = session.get("team_employee_name")
    checklist = db.execute(
        "SELECT * FROM boat_checklists WHERE id = ? AND employee_name = ?",
        (checklist_id, employee_name),
    ).fetchone()
    if checklist is None:
        return redirect(url_for("team_dashboard"))

    questions = _checklist_questions_for(checklist["checklist_type"], checklist["boat"])
    question_index_raw = request.form.get("question_index", "").strip()
    status = request.form.get("status", "").strip()
    comment = request.form.get("comment", "").strip()

    if (
        question_index_raw.isdigit()
        and status in ("ok", "problem")
        and int(question_index_raw) < len(questions)
    ):
        question_index = int(question_index_raw)
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT OR IGNORE INTO boat_checklist_answers "
            "(checklist_id, question_index, question_text, status, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (checklist_id, question_index, questions[question_index]["text"], status, comment or None, now),
        )
        # rowcount is 0 if the INSERT OR IGNORE hit the UNIQUE constraint
        # (e.g. a double submit) — lastrowid would be stale in that case,
        # so only attach photos when a row was actually just created.
        saved_photo_paths = []
        question_label = questions[question_index]["title"] or questions[question_index]["text"]
        if cur.rowcount == 1:
            answer_id = cur.lastrowid
            if status == "problem":
                defect_description = f"{question_label} — {comment}" if comment else question_label
                db.execute(
                    "INSERT INTO boat_defects (boat, checklist_id, answer_id, description, employee_name, "
                    "status, reported_at, updated_at) VALUES (?, ?, ?, ?, ?, 'new', ?, ?)",
                    (checklist["boat"], checklist_id, answer_id, defect_description, employee_name, now, now),
                )
            photos_dir = os.path.join(app.static_folder, "checklist_photos")
            for file in request.files.getlist("photos"):
                if file and file.filename:
                    ext = os.path.splitext(file.filename)[1].lower()
                    if ext in WORK_PHOTO_EXTENSIONS:
                        os.makedirs(photos_dir, exist_ok=True)
                        filename = f"{answer_id}-{secrets.token_hex(6)}{ext}"
                        filepath = os.path.join(photos_dir, filename)
                        file.save(filepath)
                        saved_photo_paths.append(filepath)
                        db.execute(
                            "INSERT INTO checklist_answer_photos (answer_id, filename, created_at) "
                            "VALUES (?, ?, ?)",
                            (answer_id, filename, now),
                        )
        db.commit()
        if status == "problem" and cur.rowcount == 1:
            checklist_label = CHECKLIST_TYPE_LABELS.get(
                checklist["checklist_type"], checklist["checklist_type"]
            )
            telegram_dispatch = dispatch_notification(
                db,
                EVENT_FLEET_CHECKLIST_PROBLEM,
                f"⚠️ <b>{html.escape(checklist_label)}</b> — {html.escape(checklist['boat'])}\n"
                f"Капитан: {html.escape(employee_name)}\n"
                f"Пункт: {html.escape(question_label)}\n"
                f"Комментарий: {html.escape(comment) if comment else '—'}",
                send_telegram_notification,
            )
            dispatch_photos(
                telegram_dispatch,
                saved_photo_paths,
                send_telegram_photo,
            )
            boat_index = next((i for i, b in enumerate(BOATS) if b["name"] == checklist["boat"]), None)
            send_push_notification(
                f"{checklist_label} — {checklist['boat']}",
                f"{question_label}" + (f": {comment}" if comment else ""),
                url=f"/fleet/{boat_index}" if boat_index is not None else "/fleet",
            )
    return redirect(url_for("team_checklist_run", checklist_id=checklist_id))


@app.route("/team/checklist/<int:checklist_id>/defects", methods=["POST"])
@team_login_required
def team_checklist_add_defects(checklist_id):
    """Free-text defects a captain noticed but that aren't covered by any
    of the fixed checklist questions — added from the "Осмотр завершён"
    screen, any number at a time, not tied to a specific question_index."""
    db = get_db()
    employee_name = session.get("team_employee_name")
    checklist = db.execute(
        "SELECT * FROM boat_checklists WHERE id = ? AND employee_name = ?",
        (checklist_id, employee_name),
    ).fetchone()
    if checklist is None:
        return redirect(url_for("team_dashboard"))

    descriptions = [d.strip() for d in request.form.getlist("defect[]") if d.strip()]
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for description in descriptions:
        db.execute(
            "INSERT INTO boat_defects (boat, checklist_id, answer_id, description, employee_name, "
            "status, reported_at, updated_at) VALUES (?, ?, NULL, ?, ?, 'new', ?, ?)",
            (checklist["boat"], checklist_id, description, employee_name, now, now),
        )
    db.commit()
    if descriptions:
        checklist_label = CHECKLIST_TYPE_LABELS.get(checklist["checklist_type"], checklist["checklist_type"])
        dispatch_notification(
            db,
            EVENT_FLEET_EXTRA_DEFECT,
            f"⚠️ <b>Неисправность вне чек-листа</b> — {html.escape(checklist['boat'])}\n"
            f"Капитан: {html.escape(employee_name)}\n"
            f"Осмотр: {html.escape(checklist_label)}\n"
            f"Описание: {html.escape('; '.join(descriptions))}",
            send_telegram_notification,
        )
        boat_index = next((i for i, b in enumerate(BOATS) if b["name"] == checklist["boat"]), None)
        send_push_notification(
            f"Неисправность вне чек-листа — {checklist['boat']}",
            "; ".join(descriptions),
            url=f"/fleet/{boat_index}" if boat_index is not None else "/fleet",
        )
    return redirect(url_for("team_checklist_run", checklist_id=checklist_id))


# ---------------------------------------------------------------------
# Аналитика — финансовая аналитика по бизнесу. Первый шаг: выписка по
# расчётному счёту из Т-Банка (см. TBANK_API_TOKEN/TBANK_ACCOUNT_NUMBER
# выше). Пока подключение не настроено, раздел просто показывает заглушку.
# ---------------------------------------------------------------------
def _tbank_request(path, params, token=None):
    resp = requests.get(
        f"{TBANK_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token or TBANK_API_TOKEN}"},
        params=params,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Т-Банк вернул ошибку {resp.status_code}: {resp.text[:500]}")
    return resp.json()


def _tbank_request_post(path, payload, token=None):
    resp = requests.post(
        f"{TBANK_API_BASE}{path}",
        headers={"Authorization": f"Bearer {token or TBANK_API_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Т-Банк вернул ошибку {resp.status_code}: {resp.text[:500]}")
    return resp.json()


# ---------------------------------------------------------------------
# Зарплаты — выплата самозанятому через Т-Банк ("Отправить в Т-Банк" на
# карточке сотрудника). ВАЖНО: подпись платёжного реестра
# (/self-employed/payment-registry/submit) в API Т-Банка требует mTLS-
# сертификата, которого у нас нет — есть только Bearer-токен. Поэтому этот
# код доводит дело только до создания ЧЕРНОВИКА реестра; подписать и
# оплатить его администратор должен сам в личном кабинете Т-Бизнес. Это
# осознанное ограничение, а не недоделка — см. обсуждение с владельцем
# бизнеса.
# ---------------------------------------------------------------------
def _tbank_recipient_full_name(r):
    """Т-Банк gives the self-employed recipient's name as three separate
    fields (confirmed against a real account — the earlier guesses of a
    single combined fullName/name/fio/... field were all wrong), not one
    combined string."""
    return " ".join(w for w in (r.get("lastName"), r.get("firstName"), r.get("middleName")) if w)


def _tbank_find_self_employed(name):
    """Look up a registered self-employed recipient in Т-Банк matching this
    employee. Our employee names are just "Имя Фамилия" — Т-Банк's records
    also carry a middleName (patronymic) — so this matches whenever every
    word of our name appears somewhere in the Т-Банк name, rather than
    requiring the two strings to be identical. Recipients with status
    "DELETED" are skipped outright — a deleted recipient can never receive
    money, and matching one instead of a real active person with the same
    name would be actively wrong. No other status filtering: which of the
    remaining statuses are actually payable is enforced by Т-Банк itself at
    registry creation. Returns the matching recipient dict. Raises
    RuntimeError (message safe to show the admin) if nobody matches — with
    the names Т-Банк actually returned, so a genuine mismatch is visible
    right away — or if more than one recipient matches, since it's too
    risky to guess which is meant."""
    data = _tbank_request_post(
        "/v1/self-employed/recipients/list", {"limit": 900}, token=TBANK_API_TOKEN_PAYMENT
    )
    recipients = data.get("recipients") or data.get("items") or data.get("data") or []
    our_words = {w.casefold() for w in name.split()}
    matches = []
    found_names = []
    for r in recipients:
        if r.get("status") == "DELETED":
            continue
        full_name = _tbank_recipient_full_name(r)
        if full_name:
            found_names.append(full_name)
        their_words = {w.casefold() for w in full_name.split()}
        if our_words and their_words and our_words.issubset(their_words):
            matches.append(r)
    if len(matches) > 1:
        raise RuntimeError(
            f"В Т-Банке найдено несколько самозанятых, подходящих под имя «{name}» — уточните вручную в Т-Бизнес."
        )
    if matches:
        return matches[0]
    if found_names:
        shown = ", ".join(found_names[:20]) + ("…" if len(found_names) > 20 else "")
        raise RuntimeError(
            f"Самозанятый «{name}» не найден среди {len(found_names)} получателей в Т-Банке: {shown}"
        )
    raise RuntimeError(
        f"Т-Банк вернул {len(recipients)} получателей, но среди них нет ни одного не удалённого "
        "с именем и фамилией — проверьте в Т-Бизнес, зарегистрирован ли этот самозанятый."
    )


def _tbank_create_payout_registry(recipient, amount, purpose):
    """Create a DRAFT self-employed payment registry in Т-Банк for one
    recipient (async: create, then poll for the result). The payload shape
    here — accountNumber + selfEmployedInfo{firstName,lastName,middleName}
    per payment, sum instead of amount, taxHolding nested per payment — is
    confirmed against a real "Illegal json" 400 response that named exactly
    these fields (an earlier recipientId/amount-only guess was wrong).
    accountNumber comes from the recipient's own bankInfo, already on the
    dict returned by _tbank_find_self_employed. Returns the new
    paymentRegistryId. Raises RuntimeError (message safe to show the admin)
    on failure or timeout."""
    account_number = (recipient.get("bankInfo") or {}).get("accountNumber")
    if not account_number:
        raise RuntimeError(
            f"У самозанятого {_tbank_recipient_full_name(recipient)} в Т-Банке не указан номер счёта "
            "для выплат — реквизиты нужно донастроить в Т-Бизнес."
        )
    correlation_id = str(uuid.uuid4())
    _tbank_request_post(
        "/v1/self-employed/payment-registry/create",
        {
            "correlationId": correlation_id,
            "companyAccountNumber": TBANK_ACCOUNT_NUMBER,
            "payments": [{
                "number": 1,
                "accountNumber": account_number,
                "paymentPurpose": purpose,
                "selfEmployedInfo": {
                    "firstName": recipient.get("firstName"),
                    "lastName": recipient.get("lastName"),
                    "middleName": recipient.get("middleName"),
                },
                "sum": amount,
                "taxHolding": False,
            }],
        },
        token=TBANK_API_TOKEN_PAYMENT,
    )
    for _ in range(10):
        result = _tbank_request(
            "/v1/self-employed/payment-registry/create/result",
            {"correlationId": correlation_id},
            token=TBANK_API_TOKEN_PAYMENT,
        )
        status = result.get("status")
        if status == "CREATED":
            return result.get("paymentRegistryId")
        if status == "ERROR":
            raise RuntimeError(result.get("errorMessage") or "Т-Банк отклонил создание реестра.")
        time.sleep(1)
    raise RuntimeError("Т-Банк не ответил на создание реестра за отведённое время — попробуйте ещё раз позже.")


def _tbank_send_payout(db, employee, period_key, amount):
    """Find the self-employed recipient matching this employee's name and
    create a draft payment registry for them, recording the outcome
    (success or failure — both are useful history) in
    tbank_payout_registries. Never raises: every failure mode from name
    lookup to the Т-Банк API is caught and stored as a row so the admin
    always sees what happened on their next page load."""
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    recipient_name = None
    recipient_inn = None
    payment_registry_id = None
    status = "error"
    error_message = None
    try:
        recipient = _tbank_find_self_employed(employee)
        recipient_name = _tbank_recipient_full_name(recipient)
        recipient_inn = recipient.get("inn")
        purpose = f"Вознаграждение за оказанные услуги, расчётный период с {period_key}"
        payment_registry_id = _tbank_create_payout_registry(recipient, amount, purpose)
        status = "created"
    except Exception as e:
        error_message = str(e)
    db.execute(
        "INSERT INTO tbank_payout_registries (employee, period_key, amount, recipient_name, "
        "recipient_inn, payment_registry_id, status, error_message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (employee, period_key, amount, recipient_name, recipient_inn,
         payment_registry_id, status, error_message, now),
    )
    db.commit()


def _tbank_fetch_operations(start_date, end_date):
    """Fetch every operation for [start_date, end_date] (YYYY-MM-DD strings),
    following cursor-based pagination. Returns the raw operation dicts —
    field names aren't fully documented (JS-rendered API docs), so parsing
    happens separately in _tbank_normalize_operation, defensively."""
    operations = []
    cursor = None
    params_base = {
        "accountNumber": TBANK_ACCOUNT_NUMBER,
        "from": f"{start_date}T00:00:00Z",
        "to": f"{end_date}T23:59:59Z",
        "limit": 1000,
    }
    while True:
        params = dict(params_base)
        if cursor:
            params["cursor"] = cursor
        data = _tbank_request("/v1/statement", params)
        batch = data.get("operations") or data.get("Operations") or []
        operations.extend(batch)
        cursor = data.get("nextCursor") or data.get("NextCursor")
        if not cursor or not batch:
            break
    return operations


def _tbank_pick(op, *keys):
    for k in keys:
        v = op.get(k)
        if v not in (None, ""):
            return v
    return None


def _tbank_normalize_operation(op):
    """Best-effort mapping from a raw T-Bank operation dict to our row shape.
    The full raw dict is always kept in raw_json regardless of how well this
    guesses field names, so nothing is lost if a name below is wrong."""
    operation_id = _tbank_pick(op, "operationId", "id", "documentNumber", "trxId", "uid")
    date_val = _tbank_pick(op, "dateTime", "date", "operationDate", "authorizationDate")

    amount_raw = _tbank_pick(op, "rubleAmount", "operationAmount", "accountAmount", "amount")
    if isinstance(amount_raw, dict):
        amount = _tbank_pick(amount_raw, "value", "amount")
    else:
        amount = amount_raw
    try:
        amount = abs(float(amount)) if amount is not None else 0.0
    except (TypeError, ValueError):
        amount = 0.0

    type_raw = str(
        _tbank_pick(op, "typeOfOperation", "type", "direction", "operationType") or ""
    ).lower()
    direction = "out" if type_raw in ("debit", "out", "expense", "outcome", "withdrawal") else "in"

    counterparty = (
        op.get("counterParty") or op.get("counterparty")
        or op.get("payer") or op.get("recipient") or op.get("receiver") or {}
    )
    if not isinstance(counterparty, dict):
        counterparty = {}
    counterparty_name = _tbank_pick(
        op, "counterpartyName", "payerName", "recipientName"
    ) or counterparty.get("name")
    counterparty_inn = _tbank_pick(
        op, "counterpartyInn", "payerInn", "recipientInn"
    ) or counterparty.get("inn")

    return {
        "operation_id": str(operation_id) if operation_id else None,
        "operation_date": str(date_val) if date_val else "",
        "amount": amount,
        "direction": direction,
        "counterparty_name": counterparty_name,
        "counterparty_inn": counterparty_inn,
        "purpose": _tbank_pick(op, "payPurpose", "paymentPurpose", "purpose", "description", "comment"),
        "category": _tbank_pick(op, "category", "categoryCode"),
        "status": _tbank_pick(op, "status", "operationStatus"),
    }


def _parse_date_filter():
    filter_start = request.args.get("start", "").strip()
    filter_end = request.args.get("end", "").strip()
    try:
        dt.date.fromisoformat(filter_start)
    except ValueError:
        filter_start = ""
    try:
        dt.date.fromisoformat(filter_end)
    except ValueError:
        filter_end = ""
    return filter_start, filter_end


def _fetch_filtered_transactions(db, filter_start, filter_end):
    if filter_start or filter_end:
        # An explicit date range means the admin is deliberately looking
        # for something specific (possibly months back) rather than just
        # the recent activity the default view covers — a much higher cap
        # than the default 200, just as a sanity limit against an
        # accidentally huge range, not a real-world ceiling.
        # operation_date isn't stored in one consistent shape (plain
        # "YYYY-MM-DD" from manual entry vs "YYYY-MM-DDTHH:MM:SSZ" from the
        # Т-Банк import) — comparing only the first 10 characters sidesteps
        # that entirely instead of guessing a time suffix to compare
        # against (e.g. a literal "T" sorts after a space, which would
        # wrongly exclude same-day ISO-with-time rows from the end bound).
        conditions = []
        params = []
        if filter_start:
            conditions.append("substr(operation_date, 1, 10) >= ?")
            params.append(filter_start)
        if filter_end:
            conditions.append("substr(operation_date, 1, 10) <= ?")
            params.append(filter_end)
        transactions = db.execute(
            f"SELECT * FROM bank_transactions WHERE {' AND '.join(conditions)} "
            "ORDER BY operation_date DESC, id DESC LIMIT 1000",
            params,
        ).fetchall()
    else:
        transactions = db.execute(
            "SELECT * FROM bank_transactions ORDER BY operation_date DESC, id DESC LIMIT 200"
        ).fetchall()
    split_rows = db.execute(
        "SELECT ts.transaction_id, ts.amount, projects.name AS project_name, "
        "tuning_order_items.work_name AS item_name "
        "FROM transaction_splits ts JOIN projects ON projects.id = ts.project_id "
        "LEFT JOIN tuning_order_items ON tuning_order_items.id = ts.item_id "
        "ORDER BY ts.id"
    ).fetchall()
    splits_by_transaction = {}
    for s in split_rows:
        splits_by_transaction.setdefault(s["transaction_id"], []).append(
            {"project_name": s["project_name"], "amount": s["amount"], "item_name": s["item_name"]}
        )
    return transactions, splits_by_transaction


def _transactions_table_context(db):
    filter_start, filter_end = _parse_date_filter()
    transactions, splits_by_transaction = _fetch_filtered_transactions(db, filter_start, filter_end)
    projects = db.execute(
        "SELECT projects.*, tuning_orders.client_name AS client_name, "
        "tuning_orders.boat_model AS boat_model, "
        "tuning_orders.boat_registration_number AS boat_registration_number, "
        "tuning_orders.equipment_type AS equipment_type, "
        "tuning_orders.motor_model AS motor_model, "
        "tuning_orders.motor_serial_number AS motor_serial_number "
        "FROM projects LEFT JOIN tuning_orders ON tuning_orders.id = projects.tuning_order_id "
        "ORDER BY projects.created_at DESC, projects.id DESC"
    ).fetchall()
    # Actions on a row (assign project, save purpose, split) redirect back
    # here afterwards — carry the active date filter along in that "next"
    # URL, or it silently resets to the unfiltered view on every single
    # action, forcing the admin to re-apply it each time.
    current_url = url_for(
        "analytics_index",
        start=filter_start or None, end=filter_end or None,
    )
    return {
        "transactions": transactions, "projects": projects,
        "splits_by_transaction": splits_by_transaction, "current_url": current_url,
        "filter_start": filter_start, "filter_end": filter_end,
        "items_by_project": _items_by_project(db),
    }


@app.route("/analytics")
@admin_login_required
def analytics_index():
    db = get_db()
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    context = _transactions_table_context(db)
    return render_template(
        "analytics.html", active_page="analytics", sub_page="transactions",
        tbank_configured=tbank_statement_configured(), today=today.isoformat(),
        fetch_default_start=week_ago.isoformat(), fetch_default_end=today.isoformat(),
        fetch_error=session.pop("tbank_fetch_error", None),
        fetch_result=session.pop("tbank_fetch_result", None),
        **context,
    )


@app.route("/analytics/transactions-fragment")
@admin_login_required
def analytics_transactions_fragment():
    """Same transactions table as the main page, rendered without the
    surrounding layout — fetched via JS when the date filter is applied,
    so that doesn't need a full page navigation."""
    db = get_db()
    context = _transactions_table_context(db)
    return render_template(
        "_transactions_table.html", tbank_configured=tbank_statement_configured(), **context
    )


@app.route("/analytics/fetch", methods=["POST"])
@admin_login_required
def analytics_fetch():
    if not tbank_statement_configured():
        return redirect(url_for("analytics_index"))

    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    try:
        dt.date.fromisoformat(start_date)
        dt.date.fromisoformat(end_date)
    except ValueError:
        session["tbank_fetch_error"] = "Некорректный период."
        return redirect(url_for("analytics_index"))

    db = get_db()
    try:
        raw_operations = _tbank_fetch_operations(start_date, end_date)
    except requests.RequestException as e:
        session["tbank_fetch_error"] = f"Ошибка соединения с Т-Банком: {e}"
        return redirect(url_for("analytics_index"))
    except RuntimeError as e:
        session["tbank_fetch_error"] = str(e)
        return redirect(url_for("analytics_index"))

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    added = 0
    updated = 0
    skipped_no_id = 0
    for op in raw_operations:
        data = _tbank_normalize_operation(op)
        if not data["operation_id"]:
            skipped_no_id += 1
            continue
        existing = db.execute(
            "SELECT 1 FROM bank_transactions WHERE operation_id = ?", (data["operation_id"],)
        ).fetchone()
        if existing:
            # Re-parse from this fresh fetch even for rows we already have —
            # if _tbank_normalize_operation's field-name guesses get fixed
            # later, previously-imported rows should self-heal on the next
            # fetch instead of staying wrong until someone deletes them.
            db.execute(
                "UPDATE bank_transactions SET operation_date=?, amount=?, direction=?, "
                "counterparty_name=?, counterparty_inn=?, purpose=?, category=?, status=?, "
                "raw_json=? WHERE operation_id=?",
                (data["operation_date"], data["amount"], data["direction"],
                 data["counterparty_name"], data["counterparty_inn"], data["purpose"],
                 data["category"], data["status"], json.dumps(op, ensure_ascii=False),
                 data["operation_id"]),
            )
            updated += 1
            continue
        db.execute(
            "INSERT INTO bank_transactions (operation_id, account_number, operation_date, amount, "
            "direction, counterparty_name, counterparty_inn, purpose, category, status, raw_json, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data["operation_id"], TBANK_ACCOUNT_NUMBER, data["operation_date"], data["amount"],
             data["direction"], data["counterparty_name"], data["counterparty_inn"],
             data["purpose"], data["category"], data["status"],
             json.dumps(op, ensure_ascii=False), now),
        )
        added += 1
    db.commit()

    session["tbank_fetch_result"] = (
        f"Получено операций: {len(raw_operations)}. Новых добавлено: {added}. "
        f"Обновлено: {updated}."
        + (f" Без ID (пропущено): {skipped_no_id}." if skipped_no_id else "")
    )
    return redirect(url_for("analytics_index"))


def _project_totals(db, project_id):
    row = db.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE 0 END), 0) AS income, "
        "COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END), 0) AS expense "
        "FROM bank_transactions "
        "WHERE project_id = ? AND id NOT IN (SELECT transaction_id FROM transaction_splits)",
        (project_id,),
    ).fetchone()
    split_row = db.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN bt.direction='in' THEN ts.amount ELSE 0 END), 0) AS income, "
        "COALESCE(SUM(CASE WHEN bt.direction='out' THEN ts.amount ELSE 0 END), 0) AS expense "
        "FROM transaction_splits ts JOIN bank_transactions bt ON bt.id = ts.transaction_id "
        "WHERE ts.project_id = ?",
        (project_id,),
    ).fetchone()
    entries_expense = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS expense FROM entries WHERE project_id = ?",
        (project_id,),
    ).fetchone()["expense"]
    materials_expense = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS expense FROM supply_writeoffs WHERE project_id = ?",
        (project_id,),
    ).fetchone()["expense"]
    payments_income = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS income FROM tuning_payments WHERE project_id = ?",
        (project_id,),
    ).fetchone()["income"]
    income = row["income"] + split_row["income"] + payments_income
    expense = row["expense"] + split_row["expense"] + entries_expense + materials_expense
    return income, expense, income - expense


def _item_profitability(db, order_id):
    """Per-work-item breakdown for one order: price (what the item is
    billed at) against its costs — material write-offs (via the existing
    tuning_item_assignments chain — team_tuning_task_writeoff_material)
    and any expense transactions explicitly linked to that item (the new
    "Весь проект"/work-item picker in _transactions_table.html and
    transaction_split.html). Income transactions are deliberately NOT
    attributed per item here: the client pays for the whole project, not
    per work item, so there's no meaningful way to split their payment
    across items — only costs can be traced to a specific one."""
    items = db.execute(
        "SELECT * FROM tuning_order_items WHERE order_id = ? ORDER BY id", (order_id,)
    ).fetchall()
    result = []
    for item in items:
        materials_expense = db.execute(
            "SELECT COALESCE(SUM(sw.amount), 0) AS expense FROM supply_writeoffs sw "
            "JOIN tuning_item_assignments tia ON tia.id = sw.tuning_item_assignment_id "
            "WHERE tia.item_id = ?",
            (item["id"],),
        ).fetchone()["expense"]
        tx_expense_direct = db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS expense FROM bank_transactions "
            "WHERE item_id = ? AND direction = 'out' "
            "AND id NOT IN (SELECT transaction_id FROM transaction_splits)",
            (item["id"],),
        ).fetchone()["expense"]
        tx_expense_split = db.execute(
            "SELECT COALESCE(SUM(ts.amount), 0) AS expense "
            "FROM transaction_splits ts JOIN bank_transactions bt ON bt.id = ts.transaction_id "
            "WHERE ts.item_id = ? AND bt.direction = 'out'",
            (item["id"],),
        ).fetchone()["expense"]
        tx_expense = tx_expense_direct + tx_expense_split
        price = item["price"] if item["status"] != "removed" else 0.0
        profit = price - materials_expense - tx_expense
        result.append({
            "item": item, "price": price, "materials_expense": materials_expense,
            "tx_expense": tx_expense, "profit": profit,
        })
    return result


@app.route("/analytics/projects")
@admin_login_required
def analytics_projects():
    db = get_db()
    rows = db.execute(
        "SELECT projects.*, tuning_orders.client_name AS client_name, "
        "tuning_orders.boat_model AS boat_model, "
        "tuning_orders.boat_registration_number AS boat_registration_number, "
        "tuning_orders.equipment_type AS equipment_type, "
        "tuning_orders.motor_model AS motor_model, "
        "tuning_orders.motor_serial_number AS motor_serial_number "
        "FROM projects LEFT JOIN tuning_orders ON tuning_orders.id = projects.tuning_order_id "
        "ORDER BY projects.created_at DESC, projects.id DESC"
    ).fetchall()
    projects = []
    for p in rows:
        income, expense, profit = _project_totals(db, p["id"])
        projects.append({
            "id": p["id"], "name": p["name"], "tuning_order_id": p["tuning_order_id"],
            "client_name": p["client_name"], "boat_model": p["boat_model"],
            "boat_registration_number": p["boat_registration_number"],
            "equipment_type": p["equipment_type"], "motor_model": p["motor_model"],
            "motor_serial_number": p["motor_serial_number"],
            "income": income, "expense": expense, "profit": profit,
        })

    now = dt.datetime.now()
    month_prefix = now.strftime("%Y-%m")
    month_row = db.execute(
        "SELECT "
        "COALESCE(SUM(CASE WHEN direction='in' THEN amount ELSE 0 END), 0) AS income, "
        "COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END), 0) AS expense "
        "FROM bank_transactions WHERE substr(operation_date, 1, 7) = ? "
        "AND (project_id IS NOT NULL OR id IN (SELECT DISTINCT transaction_id FROM transaction_splits))",
        (month_prefix,),
    ).fetchone()
    month_entries_expense = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS expense FROM entries "
        "WHERE project_id IS NOT NULL AND substr(work_date, 1, 7) = ?",
        (month_prefix,),
    ).fetchone()["expense"]
    month_materials_expense = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS expense FROM supply_writeoffs "
        "WHERE project_id IS NOT NULL AND substr(created_at, 1, 7) = ?",
        (month_prefix,),
    ).fetchone()["expense"]
    month_payments_income = db.execute(
        "SELECT COALESCE(SUM(amount), 0) AS income FROM tuning_payments "
        "WHERE project_id IS NOT NULL AND substr(paid_at, 1, 7) = ?",
        (month_prefix,),
    ).fetchone()["income"]
    month_income = month_row["income"] + month_payments_income
    month_expense = month_row["expense"] + month_entries_expense + month_materials_expense

    return render_template(
        "analytics_projects.html", active_page="analytics", sub_page="projects",
        projects=projects, month_name=MONTHS_NOM[now.month - 1], month_year=now.year,
        month_income=month_income, month_expense=month_expense,
        month_profit=month_income - month_expense,
    )


@app.route("/analytics/projects/<int:project_id>")
@admin_login_required
def project_detail(project_id):
    db = get_db()
    project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        return redirect(url_for("analytics_projects"))
    transactions = db.execute(
        "SELECT * FROM ("
        "  SELECT bt.id AS id, bt.operation_date AS operation_date, bt.direction AS direction, "
        "         bt.counterparty_name AS counterparty_name, bt.purpose AS purpose, "
        "         bt.amount AS full_amount, bt.amount AS display_amount, "
        "         0 AS is_split, NULL AS split_id "
        "  FROM bank_transactions bt "
        "  WHERE bt.project_id = ? AND bt.id NOT IN (SELECT transaction_id FROM transaction_splits) "
        "  UNION ALL "
        "  SELECT bt.id AS id, bt.operation_date AS operation_date, bt.direction AS direction, "
        "         bt.counterparty_name AS counterparty_name, bt.purpose AS purpose, "
        "         bt.amount AS full_amount, ts.amount AS display_amount, "
        "         1 AS is_split, ts.id AS split_id "
        "  FROM transaction_splits ts JOIN bank_transactions bt ON bt.id = ts.transaction_id "
        "  WHERE ts.project_id = ?"
        ") ORDER BY operation_date DESC, id DESC",
        (project_id, project_id),
    ).fetchall()
    unattached = db.execute(
        "SELECT * FROM bank_transactions WHERE project_id IS NULL "
        "AND id NOT IN (SELECT transaction_id FROM transaction_splits) "
        "ORDER BY operation_date DESC, id DESC LIMIT 300"
    ).fetchall()
    work_entries = db.execute(
        "SELECT * FROM entries WHERE project_id = ? ORDER BY work_date DESC, id DESC",
        (project_id,),
    ).fetchall()
    material_writeoffs = db.execute(
        "SELECT sw.*, sp.name AS product_name "
        "FROM supply_writeoffs sw JOIN supply_products sp ON sp.id = sw.product_id "
        "WHERE sw.project_id = ? ORDER BY sw.created_at DESC, sw.id DESC",
        (project_id,),
    ).fetchall()
    payments = db.execute(
        "SELECT * FROM tuning_payments WHERE project_id = ? ORDER BY paid_at DESC, id DESC",
        (project_id,),
    ).fetchall()
    income, expense, profit = _project_totals(db, project_id)
    order = None
    item_profitability = []
    if project["tuning_order_id"]:
        order = db.execute(
            "SELECT * FROM tuning_orders WHERE id = ?", (project["tuning_order_id"],)
        ).fetchone()
        item_profitability = _item_profitability(db, project["tuning_order_id"])
    return render_template(
        "project_detail.html", active_page="analytics", sub_page="projects",
        project=project, order=order, transactions=transactions, unattached=unattached,
        work_entries=work_entries, material_writeoffs=material_writeoffs, payments=payments,
        income=income, expense=expense, profit=profit, item_profitability=item_profitability,
    )


def _redirect_or_ajax_ok(next_url):
    # The transactions table submits these same forms via fetch() (see
    # analytics.html) so assigning a project or saving a purpose doesn't
    # reload the whole page and lose the date filter's scroll position —
    # a plain <form> submit (JS disabled, or any other caller) still gets
    # the normal redirect. request.form always has "next" set to the
    # current filtered URL either way (see current_url in app.py).
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return ("", 204)
    return redirect(next_url)


@app.route("/analytics/transactions/manual-add", methods=["POST"])
@admin_login_required
def add_manual_transaction():
    # Covers anything that never hits the bank statement — cash payments
    # most often, but any other off-account movement too. Same table as
    # the Т-Банк import (bank_transactions), just tagged source='manual'
    # so it's editable/deletable here, unlike imported rows which should
    # stay in sync with the actual statement.
    db = get_db()
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    next_url = request.form.get("next") or url_for("analytics_index")
    operation_date = request.form.get("operation_date", "").strip()
    direction = request.form.get("direction", "").strip()
    amount_raw = request.form.get("amount", "").strip().replace(",", ".")
    counterparty_name = request.form.get("counterparty_name", "").strip()
    purpose = request.form.get("purpose", "").strip()

    errors = []
    if not operation_date:
        errors.append("Укажите дату.")
    if direction not in ("in", "out"):
        errors.append("Укажите тип операции.")
    amount = None
    try:
        amount = float(amount_raw)
        if amount <= 0:
            errors.append("Сумма должна быть больше нуля.")
    except ValueError:
        errors.append("Сумма должна быть числом.")

    if errors:
        if is_ajax:
            return (" ".join(errors), 400)
        return redirect(next_url)

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    operation_id = f"manual-{secrets.token_hex(8)}"
    db.execute(
        "INSERT INTO bank_transactions (operation_id, account_number, operation_date, amount, "
        "direction, counterparty_name, purpose, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?)",
        (operation_id, "Вручную", operation_date, amount, direction,
         counterparty_name or None, purpose or None, now),
    )
    db.commit()
    return _redirect_or_ajax_ok(next_url)


@app.route("/analytics/transactions/<int:transaction_id>/delete", methods=["POST"])
@admin_login_required
def delete_manual_transaction(transaction_id):
    # Only ever touches source='manual' rows — imported bank statement
    # rows aren't deletable here, they should stay in sync with the
    # actual account instead of being hand-removed.
    db = get_db()
    row = db.execute(
        "SELECT source FROM bank_transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    if row is not None and row["source"] == "manual":
        db.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (transaction_id,))
        db.execute("DELETE FROM bank_transactions WHERE id = ?", (transaction_id,))
        db.commit()
    next_url = request.form.get("next") or url_for("analytics_index")
    return _redirect_or_ajax_ok(next_url)


@app.route("/analytics/transactions/project", methods=["POST"])
@admin_login_required
def set_transaction_project():
    db = get_db()
    transaction_id = request.form.get("transaction_id", "").strip()
    project_id, item_id = _resolve_transaction_target(db, request.form.get("target", ""))
    if transaction_id.isdigit():
        # A direct single-project assignment always overrides any prior split.
        db.execute(
            "DELETE FROM transaction_splits WHERE transaction_id = ?", (int(transaction_id),)
        )
        db.execute(
            "UPDATE bank_transactions SET project_id = ?, item_id = ? WHERE id = ?",
            (project_id, item_id, int(transaction_id)),
        )
        db.commit()
    next_url = request.form.get("next") or url_for("analytics_index")
    return _redirect_or_ajax_ok(next_url)


@app.route("/analytics/transactions/purpose", methods=["POST"])
@admin_login_required
def set_transaction_purpose():
    db = get_db()
    transaction_id = request.form.get("transaction_id", "").strip()
    purpose = request.form.get("purpose", "").strip()
    if transaction_id.isdigit():
        db.execute(
            "UPDATE bank_transactions SET purpose = ? WHERE id = ?",
            (purpose or None, int(transaction_id)),
        )
        db.commit()
    next_url = request.form.get("next") or url_for("analytics_index")
    return _redirect_or_ajax_ok(next_url)


def _normalize_transaction_split(db, transaction_id):
    """After a split row is removed, a single remaining row is no longer a
    split — collapse it back into a plain project_id assignment so it
    doesn't linger as a one-row split."""
    remaining = db.execute(
        "SELECT * FROM transaction_splits WHERE transaction_id = ?", (transaction_id,)
    ).fetchall()
    if len(remaining) == 1:
        db.execute(
            "UPDATE bank_transactions SET project_id = ?, item_id = ? WHERE id = ?",
            (remaining[0]["project_id"], remaining[0]["item_id"], transaction_id),
        )
        db.execute("DELETE FROM transaction_splits WHERE id = ?", (remaining[0]["id"],))


@app.route("/analytics/transactions/<int:transaction_id>/split", methods=["GET", "POST"])
@admin_login_required
def transaction_split(transaction_id):
    db = get_db()
    transaction = db.execute(
        "SELECT * FROM bank_transactions WHERE id = ?", (transaction_id,)
    ).fetchone()
    if transaction is None:
        return redirect(url_for("analytics_index"))

    projects = db.execute(
        "SELECT projects.*, tuning_orders.client_name AS client_name, "
        "tuning_orders.boat_model AS boat_model, "
        "tuning_orders.boat_registration_number AS boat_registration_number, "
        "tuning_orders.equipment_type AS equipment_type, "
        "tuning_orders.motor_model AS motor_model, "
        "tuning_orders.motor_serial_number AS motor_serial_number "
        "FROM projects LEFT JOIN tuning_orders ON tuning_orders.id = projects.tuning_order_id "
        "ORDER BY projects.created_at DESC, projects.id DESC"
    ).fetchall()
    items_by_project = _items_by_project(db)

    if request.method == "GET":
        next_url = request.args.get("next") or url_for("analytics_index")
        existing = db.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id = ? ORDER BY id",
            (transaction_id,),
        ).fetchall()
        splits = [
            {
                "target": f"i{s['item_id']}" if s["item_id"] else f"p{s['project_id']}",
                "amount": f"{s['amount']:.2f}".rstrip("0").rstrip("."),
            }
            for s in existing
        ]
        return render_template(
            "transaction_split.html", active_page="analytics", sub_page="transactions",
            transaction=transaction, projects=projects, items_by_project=items_by_project,
            splits=splits, next_url=next_url, errors=None,
        )

    next_url = request.form.get("next") or url_for("analytics_index")
    targets_raw = request.form.getlist("target[]")
    amounts = request.form.getlist("amount[]")

    display_rows = []
    for i in range(max(len(targets_raw), len(amounts))):
        target_raw = targets_raw[i].strip() if i < len(targets_raw) else ""
        amt_raw = amounts[i].strip() if i < len(amounts) else ""
        if not target_raw and not amt_raw:
            continue
        display_rows.append({"target": target_raw, "amount": amt_raw})

    errors = []
    parsed_rows = []
    seen_targets = set()
    for idx, r in enumerate(display_rows, start=1):
        project_id, item_id = _resolve_transaction_target(db, r["target"])
        if project_id is None:
            errors.append(f"Строка {idx}: выберите проект или работу.")
            continue
        target_key = (project_id, item_id)
        if target_key in seen_targets:
            errors.append(f"Строка {idx}: это уже указано в другой строке — объедините суммы в одну строку.")
            continue
        try:
            amt = float(r["amount"].replace(",", "."))
        except ValueError:
            errors.append(f"Строка {idx}: сумма должна быть числом.")
            continue
        if amt <= 0:
            errors.append(f"Строка {idx}: сумма должна быть больше нуля.")
            continue
        seen_targets.add(target_key)
        parsed_rows.append({"project_id": project_id, "item_id": item_id, "amount": amt})

    if not errors and len(parsed_rows) < 2:
        errors.append("Укажите минимум два получателя (проекта или работы), чтобы разбить сумму — для одного используйте обычный выбор.")

    if not errors:
        total = sum(r["amount"] for r in parsed_rows)
        expected = abs(transaction["amount"])
        if abs(total - expected) > 0.01:
            errors.append(
                f"Сумма частей ({format_money(total, 2)} ₽) не совпадает с суммой операции "
                f"({format_money(expected, 2)} ₽). Поправьте суммы так, чтобы они совпадали."
            )

    if errors:
        return render_template(
            "transaction_split.html", active_page="analytics", sub_page="transactions",
            transaction=transaction, projects=projects, items_by_project=items_by_project,
            splits=display_rows, next_url=next_url, errors=errors,
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (transaction_id,))
    for r in parsed_rows:
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, project_id, item_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (transaction_id, r["project_id"], r["item_id"], r["amount"], now),
        )
    db.execute("UPDATE bank_transactions SET project_id = NULL, item_id = NULL WHERE id = ?", (transaction_id,))
    db.commit()
    return redirect(next_url)


@app.route("/analytics/transactions/<int:transaction_id>/split/clear", methods=["POST"])
@admin_login_required
def clear_transaction_split(transaction_id):
    db = get_db()
    db.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (transaction_id,))
    db.commit()
    next_url = request.form.get("next") or url_for("analytics_index")
    return redirect(next_url)


@app.route("/analytics/transactions/split/remove", methods=["POST"])
@admin_login_required
def remove_transaction_split():
    db = get_db()
    split_id = request.form.get("split_id", "").strip()
    if split_id.isdigit():
        row = db.execute(
            "SELECT * FROM transaction_splits WHERE id = ?", (int(split_id),)
        ).fetchone()
        if row is not None:
            db.execute("DELETE FROM transaction_splits WHERE id = ?", (int(split_id),))
            _normalize_transaction_split(db, row["transaction_id"])
            db.commit()
    next_url = request.form.get("next") or url_for("analytics_index")
    return redirect(next_url)


# =======================================================================
# Снабжение
# =======================================================================

def _supply_warehouses(db):
    return db.execute("SELECT * FROM supply_warehouses ORDER BY name").fetchall()


def _maybe_create_low_stock_request(db, product_id):
    """Call after anything that decreases a product's stock. If the total
    across all warehouses has hit (or dropped below) the product's
    min_stock, raises a supply request asking to restock it — routed
    through the same Заявки на снабжение queue as employee requests, so
    admin handles it the same way (Принята → ... → Доставлено).

    Guarded by product_id so a product already sitting below threshold
    doesn't spawn a fresh request on every single write-off — marking the
    existing one "Доставлено" is what re-arms the check."""
    product = db.execute("SELECT * FROM supply_products WHERE id = ?", (product_id,)).fetchone()
    if product is None or not product["min_stock"]:
        return
    total = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS total FROM supply_stock WHERE product_id = ?",
        (product_id,),
    ).fetchone()["total"]
    if total > product["min_stock"]:
        return
    existing = db.execute(
        "SELECT 1 FROM supply_requests WHERE product_id = ? AND status != 'delivered'",
        (product_id,),
    ).fetchone()
    if existing:
        return
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    shortfall = max(1, product["min_stock"] - total)
    cur = db.execute(
        "INSERT INTO supply_requests (employee_name, status, created_at, product_id) VALUES (?, ?, ?, ?)",
        (SUPPLY_LOW_STOCK_REQUESTER, DEFAULT_SUPPLY_REQUEST_STATUS, now, product_id),
    )
    db.execute(
        "INSERT INTO supply_request_items (request_id, item_name, quantity) VALUES (?, ?, ?)",
        (cur.lastrowid, product["name"], shortfall),
    )
    db.commit()


@app.route("/supply/warehouses")
@admin_login_required
def supply_warehouses():
    db = get_db()
    warehouses = db.execute(
        "SELECT sw.*, "
        "(SELECT COALESCE(SUM(quantity), 0) FROM supply_stock WHERE warehouse_id = sw.id) AS total_quantity, "
        "(SELECT COUNT(DISTINCT product_id) FROM supply_stock WHERE warehouse_id = sw.id AND quantity > 0) AS product_count "
        "FROM supply_warehouses sw ORDER BY sw.name"
    ).fetchall()
    return render_template(
        "supply_warehouses.html", active_page="supply", sub_page="warehouses",
        warehouses=warehouses, warehouse_error=session.pop("warehouse_error", None),
    )


@app.route("/supply/warehouses/<int:warehouse_id>")
@admin_login_required
def supply_warehouse(warehouse_id):
    db = get_db()
    warehouse = db.execute("SELECT * FROM supply_warehouses WHERE id = ?", (warehouse_id,)).fetchone()
    if warehouse is None:
        return redirect(url_for("supply_warehouses"))
    stock = db.execute(
        "SELECT supply_stock.*, supply_products.name AS product_name, supply_products.sku AS product_sku, "
        "supply_products.photo_filename AS product_photo "
        "FROM supply_stock JOIN supply_products ON supply_products.id = supply_stock.product_id "
        "WHERE supply_stock.warehouse_id = ? AND supply_stock.quantity > 0 "
        "ORDER BY supply_products.name",
        (warehouse_id,),
    ).fetchall()
    total_quantity = sum(s["quantity"] for s in stock)
    return render_template(
        "supply_warehouse.html", active_page="supply", sub_page="warehouses",
        warehouse=warehouse, stock=stock, total_quantity=total_quantity,
    )


@app.route("/supply/warehouses/add", methods=["POST"])
@admin_login_required
def add_supply_warehouse():
    db = get_db()
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()
    if not name:
        session["warehouse_error"] = "Укажите название склада."
        return redirect(url_for("supply_warehouses"))
    db.execute(
        "INSERT INTO supply_warehouses (name, address, created_at) VALUES (?, ?, ?)",
        (name, address or None, dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    db.commit()
    return redirect(url_for("supply_warehouses"))


@app.route("/admin/import-moysklad-catalog")
@admin_login_required
def import_moysklad_catalog():
    # One-off, idempotent import of scripts/moysklad_import.json (the
    # parsed МойСклад "Остатки" report) — a route rather than a standalone
    # script because the host's SSH shell has a glibc too old for the
    # app's own venv Python, while this in-process route runs under
    # whatever interpreter Passenger already uses to serve the site.
    db = get_db()
    data_path = os.path.join(app.root_path, "scripts", "moysklad_import.json")
    with open(data_path, encoding="utf-8") as f:
        items = json.load(f)

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    warehouse_name = "Тюнинг Порзолово"
    warehouse = db.execute(
        "SELECT id FROM supply_warehouses WHERE name = ?", (warehouse_name,)
    ).fetchone()
    if warehouse is None:
        cur = db.execute(
            "INSERT INTO supply_warehouses (name, address, created_at) VALUES (?, ?, ?)",
            (warehouse_name, None, now),
        )
        warehouse_id = cur.lastrowid
        warehouse_created = True
    else:
        warehouse_id = warehouse["id"]
        warehouse_created = False

    products_created = 0
    products_existing = 0
    stock_added = 0
    stock_existing = 0

    for item in items:
        existing = db.execute(
            "SELECT id FROM supply_products WHERE sku = ?", (item["sku"],)
        ).fetchone()
        if existing is None:
            cur = db.execute(
                "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, "
                "cost_price, cost_unit, sale_price, min_stock, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item["name"], item["sku"], item["description"], None, None,
                 0, item["cost_unit"], 0, None, now),
            )
            product_id = cur.lastrowid
            products_created += 1
        else:
            product_id = existing["id"]
            products_existing += 1

        existing_stock = db.execute(
            "SELECT id FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
            (product_id, warehouse_id),
        ).fetchone()
        if existing_stock is None:
            db.execute(
                "INSERT INTO supply_stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)",
                (product_id, warehouse_id, item["quantity"]),
            )
            stock_added += 1
        else:
            stock_existing += 1

    db.commit()

    lines = [
        f"Склад «{warehouse_name}» {'создан' if warehouse_created else 'уже существовал'} (id={warehouse_id}).",
        f"Товаров в файле: {len(items)}",
        f"Товаров создано: {products_created}",
        f"Товаров уже было в каталоге (по артикулу): {products_existing}",
        f"Остатков проставлено: {stock_added}",
        f"Остатков уже было на этом складе (пропущено): {stock_existing}",
    ]
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


@app.route("/supply/catalog")
@admin_login_required
def supply_catalog():
    db = get_db()
    products = db.execute(
        "SELECT sp.*, "
        "(SELECT COALESCE(SUM(quantity), 0) FROM supply_stock WHERE product_id = sp.id) AS total_quantity "
        "FROM supply_products sp ORDER BY sp.created_at DESC, sp.id DESC"
    ).fetchall()
    return render_template(
        "supply_catalog.html", active_page="supply", sub_page="catalog",
        products=products, cost_units=SUPPLY_COST_UNITS,
        product_error=session.pop("product_error", None),
    )


@app.route("/supply/catalog/add", methods=["POST"])
@admin_login_required
def add_supply_product():
    db = get_db()
    name = request.form.get("name", "").strip()
    sku = request.form.get("sku", "").strip()
    description = request.form.get("description", "").strip()
    supplier = request.form.get("supplier", "").strip()
    cost_price_raw = request.form.get("cost_price", "").strip().replace(",", ".")
    cost_unit = request.form.get("cost_unit", "").strip()
    sale_price_raw = request.form.get("sale_price", "").strip().replace(",", ".")
    min_stock_raw = request.form.get("min_stock", "").strip().replace(",", ".")

    errors = []
    if not name:
        errors.append("Укажите название товара.")
    if cost_unit not in [u["value"] for u in SUPPLY_COST_UNITS]:
        errors.append("Укажите единицу измерения себестоимости.")

    cost_price = None
    try:
        cost_price = float(cost_price_raw)
        if cost_price < 0:
            errors.append("Себестоимость не может быть отрицательной.")
    except ValueError:
        errors.append("Себестоимость должна быть числом.")

    sale_price = None
    try:
        sale_price = float(sale_price_raw)
        if sale_price < 0:
            errors.append("Цена продажи не может быть отрицательной.")
    except ValueError:
        errors.append("Цена продажи должна быть числом.")

    # Optional — leave blank for "no automatic restock request".
    min_stock = None
    if min_stock_raw:
        try:
            min_stock = float(min_stock_raw)
            if min_stock < 0:
                errors.append("Минимальный остаток не может быть отрицательным.")
        except ValueError:
            errors.append("Минимальный остаток должен быть числом.")

    if errors:
        session["product_error"] = " ".join(errors)
        return redirect(url_for("supply_catalog"))

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = db.execute(
        "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, "
        "cost_price, cost_unit, sale_price, min_stock, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, sku or None, description or None, supplier or None, None,
         cost_price, cost_unit, sale_price, min_stock, now),
    )
    product_id = cur.lastrowid

    # Photo is optional and only ever attached after the row exists — its
    # filename is keyed on product_id, same convention as
    # upload_tuning_item_photo (work_photos/<item_id>-<random>.<ext>).
    file = request.files.get("photo")
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext in SUPPLY_PHOTO_EXTENSIONS:
            photos_dir = os.path.join(app.static_folder, "supply_photos")
            os.makedirs(photos_dir, exist_ok=True)
            filename = f"{product_id}-{secrets.token_hex(6)}{ext}"
            file.save(os.path.join(photos_dir, filename))
            db.execute(
                "UPDATE supply_products SET photo_filename = ? WHERE id = ?",
                (filename, product_id),
            )

    db.commit()
    return redirect(url_for("supply_catalog"))


@app.route("/supply/catalog/<int:product_id>")
@admin_login_required
def supply_product(product_id):
    db = get_db()
    product = db.execute("SELECT * FROM supply_products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        return redirect(url_for("supply_catalog"))

    stock = db.execute(
        "SELECT supply_stock.*, supply_warehouses.name AS warehouse_name "
        "FROM supply_stock JOIN supply_warehouses ON supply_warehouses.id = supply_stock.warehouse_id "
        "WHERE supply_stock.product_id = ? AND supply_stock.quantity > 0 "
        "ORDER BY supply_warehouses.name",
        (product_id,),
    ).fetchall()
    total_quantity = sum(s["quantity"] for s in stock)

    receipts = db.execute(
        "SELECT supply_receipts.*, supply_warehouses.name AS warehouse_name "
        "FROM supply_receipts JOIN supply_warehouses ON supply_warehouses.id = supply_receipts.warehouse_id "
        "WHERE supply_receipts.product_id = ? ORDER BY supply_receipts.id DESC LIMIT 20",
        (product_id,),
    ).fetchall()
    writeoffs = db.execute(
        "SELECT supply_writeoffs.*, supply_warehouses.name AS warehouse_name "
        "FROM supply_writeoffs JOIN supply_warehouses ON supply_warehouses.id = supply_writeoffs.warehouse_id "
        "WHERE supply_writeoffs.product_id = ? ORDER BY supply_writeoffs.id DESC LIMIT 20",
        (product_id,),
    ).fetchall()
    # Merge the two journals into one chronological feed — showing all
    # receipts before all write-offs (their separate, independently-sorted
    # queries) would misorder anything but the simplest history.
    history = sorted(
        [{"kind": "receipt", "row": r} for r in receipts] +
        [{"kind": "writeoff", "row": w} for w in writeoffs],
        key=lambda h: (h["row"]["created_at"], h["row"]["id"]),
        reverse=True,
    )[:20]

    return render_template(
        "supply_product.html", active_page="supply", sub_page="catalog",
        product=product, stock=stock, total_quantity=total_quantity,
        history=history,
        warehouses=_supply_warehouses(db),
        cost_units=SUPPLY_COST_UNITS, writeoff_reasons=SUPPLY_WRITEOFF_REASONS,
        custom_value=CUSTOM_VALUE,
        receive_error=session.pop("receive_error", None),
        writeoff_error=session.pop("writeoff_error", None),
        edit_error=session.pop("edit_error", None),
    )


@app.route("/supply/catalog/<int:product_id>/min-stock", methods=["POST"])
@admin_login_required
def set_supply_product_min_stock(product_id):
    db = get_db()
    product = db.execute("SELECT id FROM supply_products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        return redirect(url_for("supply_catalog"))
    raw = request.form.get("min_stock", "").strip().replace(",", ".")
    if not raw:
        db.execute("UPDATE supply_products SET min_stock = NULL WHERE id = ?", (product_id,))
        db.commit()
        return redirect(url_for("supply_product", product_id=product_id))
    try:
        min_stock = float(raw)
    except ValueError:
        return redirect(url_for("supply_product", product_id=product_id))
    if min_stock < 0:
        return redirect(url_for("supply_product", product_id=product_id))
    db.execute("UPDATE supply_products SET min_stock = ? WHERE id = ?", (min_stock, product_id))
    db.commit()
    # Raising/lowering the bar can itself cross the threshold — check right
    # away instead of waiting for the next write-off.
    _maybe_create_low_stock_request(db, product_id)
    return redirect(url_for("supply_product", product_id=product_id))


@app.route("/supply/catalog/<int:product_id>/edit", methods=["POST"])
@admin_login_required
def edit_supply_product(product_id):
    db = get_db()
    product = db.execute("SELECT id FROM supply_products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        return redirect(url_for("supply_catalog"))

    name = request.form.get("name", "").strip()
    sku = request.form.get("sku", "").strip()
    description = request.form.get("description", "").strip()
    supplier = request.form.get("supplier", "").strip()
    cost_price_raw = request.form.get("cost_price", "").strip().replace(",", ".")
    cost_unit = request.form.get("cost_unit", "").strip()
    sale_price_raw = request.form.get("sale_price", "").strip().replace(",", ".")

    errors = []
    if not name:
        errors.append("Укажите название товара.")
    if cost_unit not in [u["value"] for u in SUPPLY_COST_UNITS]:
        errors.append("Укажите единицу измерения себестоимости.")

    cost_price = None
    try:
        cost_price = float(cost_price_raw)
        if cost_price < 0:
            errors.append("Себестоимость не может быть отрицательной.")
    except ValueError:
        errors.append("Себестоимость должна быть числом.")

    sale_price = None
    try:
        sale_price = float(sale_price_raw)
        if sale_price < 0:
            errors.append("Цена продажи не может быть отрицательной.")
    except ValueError:
        errors.append("Цена продажи должна быть числом.")

    if errors:
        session["edit_error"] = " ".join(errors)
        return redirect(url_for("supply_product", product_id=product_id))

    db.execute(
        "UPDATE supply_products SET name = ?, sku = ?, description = ?, supplier = ?, "
        "cost_price = ?, cost_unit = ?, sale_price = ? WHERE id = ?",
        (name, sku or None, description or None, supplier or None,
         cost_price, cost_unit, sale_price, product_id),
    )

    file = request.files.get("photo")
    if file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext in SUPPLY_PHOTO_EXTENSIONS:
            photos_dir = os.path.join(app.static_folder, "supply_photos")
            os.makedirs(photos_dir, exist_ok=True)
            filename = f"{product_id}-{secrets.token_hex(6)}{ext}"
            file.save(os.path.join(photos_dir, filename))
            db.execute(
                "UPDATE supply_products SET photo_filename = ? WHERE id = ?",
                (filename, product_id),
            )

    db.commit()
    return redirect(url_for("supply_product", product_id=product_id))


@app.route("/supply/catalog/<int:product_id>/receive", methods=["POST"])
@admin_login_required
def receive_supply_product(product_id):
    db = get_db()
    product = db.execute("SELECT id FROM supply_products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        return redirect(url_for("supply_catalog"))

    warehouse_id_raw = request.form.get("warehouse_id", "").strip()
    quantity_raw = request.form.get("quantity", "").strip().replace(",", ".")
    zone = request.form.get("zone", "").strip()
    rack = request.form.get("rack", "").strip()
    spot = request.form.get("spot", "").strip()
    note = request.form.get("note", "").strip()

    errors = []
    warehouse_id = int(warehouse_id_raw) if warehouse_id_raw.isdigit() else None
    if warehouse_id is None:
        errors.append("Выберите склад.")

    quantity = None
    try:
        quantity = float(quantity_raw)
        if quantity <= 0:
            errors.append("Количество должно быть больше нуля.")
    except ValueError:
        errors.append("Количество должно быть числом.")

    if errors:
        session["receive_error"] = " ".join(errors)
        return redirect(url_for("supply_product", product_id=product_id))

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "INSERT INTO supply_receipts (product_id, warehouse_id, quantity, zone, rack, spot, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (product_id, warehouse_id, quantity, zone or None, rack or None, spot or None, note or None, now),
    )

    # Plain select-then-insert/update, not "ON CONFLICT ... DO UPDATE" — that
    # SQLite syntax needs 3.24+, not guaranteed on this host (see the
    # push_subscriptions/defect_assignments upserts elsewhere in this file).
    existing = db.execute(
        "SELECT id FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
        (product_id, warehouse_id),
    ).fetchone()
    if existing is None:
        db.execute(
            "INSERT INTO supply_stock (product_id, warehouse_id, quantity, zone, rack, spot) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (product_id, warehouse_id, quantity, zone or None, rack or None, spot or None),
        )
    else:
        # A fresh receipt's address (if given) replaces the stored one —
        # the stock row always reflects where the item was put most
        # recently, the receipt journal keeps the full history.
        db.execute(
            "UPDATE supply_stock SET quantity = quantity + ?, "
            "zone = COALESCE(?, zone), rack = COALESCE(?, rack), spot = COALESCE(?, spot) WHERE id = ?",
            (quantity, zone or None, rack or None, spot or None, existing["id"]),
        )
    db.commit()
    return redirect(url_for("supply_product", product_id=product_id))


@app.route("/supply/catalog/<int:product_id>/writeoff", methods=["POST"])
@admin_login_required
def writeoff_supply_product(product_id):
    db = get_db()
    product = db.execute("SELECT id FROM supply_products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        return redirect(url_for("supply_catalog"))

    warehouse_id_raw = request.form.get("warehouse_id", "").strip()
    quantity_raw = request.form.get("quantity", "").strip().replace(",", ".")
    reason = request.form.get("reason", "").strip()
    if reason == CUSTOM_VALUE:
        reason = request.form.get("reason_custom", "").strip()
    note = request.form.get("note", "").strip()

    errors = []
    warehouse_id = int(warehouse_id_raw) if warehouse_id_raw.isdigit() else None
    if warehouse_id is None:
        errors.append("Выберите склад.")
    if not reason:
        errors.append("Укажите причину списания.")

    stock_row = None
    if warehouse_id is not None:
        stock_row = db.execute(
            "SELECT * FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
            (product_id, warehouse_id),
        ).fetchone()

    quantity = None
    try:
        quantity = float(quantity_raw)
        if quantity <= 0:
            errors.append("Количество должно быть больше нуля.")
        elif stock_row is None or quantity > stock_row["quantity"]:
            errors.append("Нельзя списать больше, чем есть на этом складе.")
    except ValueError:
        errors.append("Количество должно быть числом.")

    if errors:
        session["writeoff_error"] = " ".join(errors)
        return redirect(url_for("supply_product", product_id=product_id))

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute(
        "INSERT INTO supply_writeoffs (product_id, warehouse_id, quantity, reason, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, warehouse_id, quantity, reason, note or None, now),
    )
    db.execute(
        "UPDATE supply_stock SET quantity = quantity - ? WHERE id = ?",
        (quantity, stock_row["id"]),
    )
    db.commit()
    _maybe_create_low_stock_request(db, product_id)
    return redirect(url_for("supply_product", product_id=product_id))


@app.route("/supply/requests")
@admin_login_required
def supply_requests():
    db = get_db()
    items_by_request = {}
    for row in db.execute("SELECT * FROM supply_request_items ORDER BY id").fetchall():
        items_by_request.setdefault(row["request_id"], []).append(row)
    requests = []
    for r in db.execute("SELECT * FROM supply_requests ORDER BY created_at DESC, id DESC").fetchall():
        req = dict(r)
        req["lines"] = items_by_request.get(r["id"], [])
        requests.append(req)
    return render_template(
        "supply_requests.html", active_page="supply", sub_page="requests",
        requests=requests, request_statuses=SUPPLY_REQUEST_STATUSES,
    )


@app.route("/supply/requests/<int:request_id>/status", methods=["POST"])
@admin_login_required
def set_supply_request_status(request_id):
    db = get_db()
    req = db.execute("SELECT * FROM supply_requests WHERE id = ?", (request_id,)).fetchone()
    if req is None:
        return redirect(url_for("supply_requests"))
    status = request.form.get("status", "").strip()
    comment = request.form.get("comment", "").strip()
    if status in [s["value"] for s in SUPPLY_REQUEST_STATUSES]:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "UPDATE supply_requests SET status = ?, status_comment = ?, updated_at = ? WHERE id = ?",
            (status, comment or None, now, request_id),
        )
        db.commit()
        label = next((s["label"] for s in SUPPLY_REQUEST_STATUSES if s["value"] == status), status)
        items = db.execute(
            "SELECT item_name, quantity FROM supply_request_items WHERE request_id = ? ORDER BY id",
            (request_id,),
        ).fetchall()
        items_text = "\n".join(f"— {html.escape(it['item_name'])} × {it['quantity']:g}" for it in items)
        text = f"📦 Заявка на снабжение: <b>{html.escape(label)}</b>\n{items_text}"
        if comment:
            text += f"\n\nКомментарий: {html.escape(comment)}"
        send_telegram_notification_to_employee(db, req["employee_name"], text)
    return redirect(url_for("supply_requests"))


def _fuel_sync_start_date(db, now):
    """Start of the fuel recovery window, including interrupted cron runs."""
    start = now.date() - dt.timedelta(days=2)
    cursor_row = db.execute(
        "SELECT MIN(COALESCE(last_synced_at, activated_at)) AS sync_cursor "
        "FROM boat_fuel_state WHERE activated_at IS NOT NULL"
    ).fetchone()
    if cursor_row and cursor_row["sync_cursor"]:
        try:
            cursor_date = dt.datetime.fromisoformat(cursor_row["sync_cursor"]).date()
            start = min(start, cursor_date - dt.timedelta(days=1))
        except (TypeError, ValueError):
            pass
    return start


def _trip_sync_start_date(db, now):
    """Start of the trip-import window with overlap and outage recovery."""
    start = now.date() - dt.timedelta(days=7)
    row = db.execute(
        "SELECT last_success_at FROM yclients_sync_state WHERE sync_key = ?",
        ("trip_import",),
    ).fetchone()
    if row and row["last_success_at"]:
        try:
            cursor_date = dt.datetime.fromisoformat(row["last_success_at"]).date()
            start = min(
                now.date() - dt.timedelta(days=2),
                cursor_date - dt.timedelta(days=1),
            )
        except (TypeError, ValueError):
            pass
    return start


def _yclients_record_has_finished(record, now):
    """Whether a booking has ended in the branch-local YCLIENTS clock."""
    raw = _yclients_record_datetime(record)
    try:
        started = dt.datetime.fromisoformat(raw)
        duration = int(record.get("seance_length") or record.get("length") or 0)
    except (TypeError, ValueError):
        return False
    if started.tzinfo is not None:
        # Match fuel accounting: timestamps from YCLIENTS represent the
        # branch wall clock. Do not let the production server timezone move
        # a trip across the completion boundary.
        started = started.replace(tzinfo=None)
    local_now = now.replace(tzinfo=None) if now.tzinfo is not None else now
    return duration > 0 and started + dt.timedelta(seconds=duration) <= local_now


def _yclients_completed_records(records, now):
    """Finished records eligible for automatic income import at this run."""
    completed = []
    for record in records:
        try:
            no_show = int(record.get("attendance")) == -1
        except (TypeError, ValueError):
            no_show = False
        if record.get("deleted") or no_show or _yclients_record_is_blocker(record):
            continue
        if _yclients_record_has_finished(record, now):
            completed.append(record)
    return completed


def _sync_fuel_from_yclients(db, now=None):
    """Fetch an overlap window, including any gap since the last successful sync."""
    now = now or dt.datetime.now()
    start_date = _fuel_sync_start_date(db, now).isoformat()
    end_date = now.date().isoformat()
    records = yclients_get_records(start_date, end_date)
    activity_ids = {record["activity_id"] for record in records if record.get("activity_id")}
    activity_colors = yclients_get_activity_colors(activity_ids)
    return fuel_services.sync_yclients_records(db, records, activity_colors, now)


def _sync_hourly_yclients(db, now=None):
    """Synchronize completed trips/income and fuel with one YCLIENTS fetch."""
    now = (now or dt.datetime.now()).replace(second=0, microsecond=0)
    start = min(_trip_sync_start_date(db, now), _fuel_sync_start_date(db, now))
    start_date = start.isoformat()
    end_date = now.date().isoformat()
    records = yclients_get_records(start_date, end_date)
    activity_ids = {record["activity_id"] for record in records if record.get("activity_id")}
    activity_colors = yclients_get_activity_colors(activity_ids)

    payroll_schedule = {
        "staffed_days": set(),
        "checked_employees": set(),
        "failed_employees": [],
        "start_date": None,
        "end_date": None,
    }
    schedule_error = None
    try:
        payroll_schedule = _yclients_payroll_schedule(
            db, start_date, end_date, today=now.date()
        )
        if payroll_schedule.get("failed_employees"):
            schedule_error = (
                "график получен не для всех сотрудников: "
                + ", ".join(payroll_schedule["failed_employees"])
            )
    except (requests.RequestException, RuntimeError, ValueError) as error:
        # Keep income/fuel live even if the separate schedule endpoint is
        # temporarily unavailable. Crucially, without an authoritative
        # response we do not delete any existing minimum-rate top-ups.
        schedule_error = str(error)
        print(f"YCLIENTS payroll schedule sync failed: {error}", file=sys.stderr, flush=True)

    completed_records = _yclients_completed_records(records, now)
    trip_stats = _import_yclients_trip_records(
        db,
        completed_records,
        activity_colors,
        start_date,
        end_date,
        now=now,
        prune_stale=False,
        scheduled_staff_days=payroll_schedule["staffed_days"],
        checked_schedule_employees=payroll_schedule["checked_employees"],
        schedule_start_date=payroll_schedule["start_date"],
        schedule_end_date=payroll_schedule["end_date"],
        minimum_shift_records=records,
        reconciliation_records=records,
        reconcile_missing=True,
    )
    fuel_stats = fuel_services.sync_yclients_records(
        db, records, activity_colors, now
    )
    db.execute(
        "INSERT INTO yclients_sync_state (sync_key, last_success_at) VALUES (?, ?) "
        "ON CONFLICT(sync_key) DO UPDATE SET last_success_at = excluded.last_success_at",
        ("trip_import", now.strftime("%Y-%m-%d %H:%M")),
    )
    db.commit()
    return {
        "start_date": start_date,
        "end_date": end_date,
        "trips": trip_stats,
        "fuel": fuel_stats,
        "schedule": {
            "staffed_days": len(payroll_schedule["staffed_days"]),
            "checked_employees": len(payroll_schedule["checked_employees"]),
            "failed_employees": len(payroll_schedule.get("failed_employees") or []),
            "error": schedule_error,
        },
    }


@app.route("/fleet/fuel/sync", methods=["POST"])
@admin_login_required
def fuel_sync_now():
    try:
        boat_index = int(request.form.get("boat_index", "0"))
    except ValueError:
        boat_index = 0
    if not (0 <= boat_index < len(BOATS)):
        boat_index = 0

    if not yclients_configured():
        session["fuel_notice"] = {
            "type": "error",
            "message": "YCLIENTS не настроен на сервере.",
        }
        return redirect(url_for("fleet.boat_detail", boat_index=boat_index))

    try:
        stats = _sync_fuel_from_yclients(get_db())
    except (requests.RequestException, RuntimeError, ValueError) as error:
        session["fuel_notice"] = {
            "type": "error",
            "message": f"Не удалось обновить топливо из YCLIENTS: {error}",
        }
    else:
        session["fuel_notice"] = {
            "type": "success",
            "message": (
                f"YCLIENTS обновлён: автоматических списаний — {stats['automatic']}, "
                f"индивидуальных рейсов ожидают расхода — {stats['pending']}."
            ),
        }
    return redirect(url_for("fleet.boat_detail", boat_index=boat_index))


@app.route("/internal/cron/sync-fuel")
def cron_sync_fuel():
    """Hourly Beget cron target for trips, income, fuel and task reminders."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    db = get_db()
    reminder_stats = send_due_task_reminders(
        db, send_telegram_notification_to_employee
    )
    if not yclients_configured():
        return (
            "yclients not configured; "
            f"task reminders: {reminder_stats['sent_3h']} after 3h, "
            f"{reminder_stats['sent_6h']} after 6h",
            503,
        )
    try:
        stats = _sync_hourly_yclients(db)
    except (requests.RequestException, RuntimeError, ValueError) as error:
        return f"error: {error}", 502
    trip_stats = stats["trips"]
    fuel_stats = stats["fuel"]
    schedule_stats = stats.get("schedule") or {}
    schedule_summary = (
        "schedule unavailable"
        if schedule_stats.get("error")
        else f"{schedule_stats.get('staffed_days', 0)} scheduled staff-days"
    )
    return (
        f"ok: {trip_stats['imported']} trips imported, "
        f"{trip_stats.get('cancelled', 0)} cancelled trips removed, "
        f"{trip_stats.get('deleted', 0)} deleted trips removed, "
        f"{trip_stats.get('payroll_updated', 0)} payroll assignments updated, "
        f"{trip_stats.get('topups_changed', 0)} minimum-rate top-ups updated, "
        f"{trip_stats['pending']} trips pending review; "
        f"schedule: {schedule_summary}; "
        f"fuel: {fuel_stats['automatic']} automatic, "
        f"{fuel_stats.get('cancelled', 0)} cancelled removed, "
        f"{fuel_stats['pending']} pending, {fuel_stats['skipped']} skipped; "
        f"task reminders: {reminder_stats['sent_3h']} after 3h, "
        f"{reminder_stats['sent_6h']} after 6h",
        200,
    )


@app.route("/internal/cron/send-task-reminders")
def cron_send_task_reminders():
    """Standalone idempotent task-reminder target for diagnostics or cron."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    stats = send_due_task_reminders(
        get_db(), send_telegram_notification_to_employee
    )
    return (
        f"ok: {stats['sent_3h']} reminder(s) after 3h, "
        f"{stats['sent_6h']} reminder(s) after 6h",
        200,
    )


def _sync_captain_shifts_for_date(db, target_date):
    """Fetch Yclients records for target_date, work out which staffed
    names are captains we know locally, and make captain_shifts for that
    date match exactly (add missing, drop stale) — safe to re-run.
    Returns the number of captains found on shift that date."""
    records = yclients_get_records(target_date, target_date)
    staffed_names = set()
    for r in records:
        if r.get("deleted") or _yclients_record_is_blocker(r):
            continue
        name = (r.get("staff") or {}).get("name", "").strip()
        if name:
            staffed_names.add(name)

    captain_rows = db.execute(
        "SELECT employees.id, employees.name FROM employees "
        "JOIN employee_positions ON employee_positions.employee_id = employees.id "
        "WHERE employee_positions.position = 'Капитан' "
        "AND employees.deleted_at IS NULL"
    ).fetchall()
    captains_on_shift = [row for row in captain_rows if row["name"] in staffed_names]

    existing_employee_ids = {
        row["employee_id"] for row in db.execute(
            "SELECT employee_id FROM captain_shifts WHERE shift_date = ?", (target_date,)
        ).fetchall()
    }
    wanted_employee_ids = {row["id"] for row in captains_on_shift}

    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for employee_id in wanted_employee_ids - existing_employee_ids:
        db.execute(
            "INSERT OR IGNORE INTO captain_shifts (employee_id, shift_date, created_at) "
            "VALUES (?, ?, ?)",
            (employee_id, target_date, now_str),
        )
    for employee_id in existing_employee_ids - wanted_employee_ids:
        db.execute(
            "DELETE FROM captain_shifts WHERE employee_id = ? AND shift_date = ?",
            (employee_id, target_date),
        )
    db.commit()
    return len(captains_on_shift)


@app.route("/internal/cron/check-captain-shifts")
def cron_check_captain_shifts():
    """Hit once a day by a cron job on the host (see README) — checks
    tomorrow's Yclients bookings and records which captains are staffed,
    so their team dashboards can show the checklist buttons. Protected by
    CRON_SECRET instead of a login, since cron has no session."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    if not yclients_configured():
        return "yclients not configured", 503

    tomorrow = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    db = get_db()
    try:
        count = _sync_captain_shifts_for_date(db, tomorrow)
    except (requests.RequestException, RuntimeError, ValueError) as e:
        return f"error: {e}", 502
    return f"ok: {count} captain(s) on shift {tomorrow}", 200


@app.route("/internal/cron/send-note-reminders")
def cron_send_note_reminders():
    """Hit every few minutes by a cron job on the host — sends a Telegram
    message for every order-note reminder whose time has come and marks it
    sent, so it's never sent twice. Protected by CRON_SECRET instead of a
    login, since cron has no session."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403

    db = get_db()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    due = db.execute(
        "SELECT r.*, n.order_id, n.text AS note_text, o.client_name, "
        "o.equipment_type, o.boat_model, o.boat_registration_number, "
        "o.motor_model, o.motor_serial_number, e.name AS remind_employee_name "
        "FROM tuning_order_note_reminders r "
        "JOIN tuning_order_notes n ON n.id = r.note_id "
        "JOIN tuning_orders o ON o.id = n.order_id "
        "LEFT JOIN employees e ON e.id = r.remind_employee_id "
        "WHERE r.sent_at IS NULL AND r.remind_at <= ?",
        (now,),
    ).fetchall()

    sent = 0
    for r in due:
        equipment_label = tuning_equipment_label(r)
        text = (
            f"⏰ <b>Напоминание по заказу №{r['order_id']}</b>\n"
            f"Клиент: {html.escape(r['client_name'])} ({html.escape(equipment_label)})\n\n"
            f"{html.escape(r['note_text'])}"
        )
        if r["remind_employee_id"] is not None:
            send_telegram_notification_to_employee(
                db, r["remind_employee_name"] or "", text
            )
        else:
            send_telegram_notification_to_admin(db, r["remind_admin_id"], text)
        db.execute(
            "UPDATE tuning_order_note_reminders SET sent_at = ? WHERE id = ?",
            (now, r["id"]),
        )
        sent += 1
    db.commit()
    return f"ok: {sent} reminder(s) sent", 200


@app.route("/internal/telegram-test")
def telegram_test():
    """Visit this URL (with the right token) to see exactly what happens
    when a Telegram notification is sent — no log-hunting required. Same
    CRON_SECRET as the shift-check endpoint, just to avoid a second secret.
    Add &target=approval to test TELEGRAM_APPROVAL_CHAT_ID instead of the
    default TELEGRAM_CHAT_ID. Add &photo=1 to test sendPhoto (a real 1x1
    PNG, not sendMessage) instead of a text notification."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    target = request.args.get("target", "default")
    chat_id = TELEGRAM_APPROVAL_CHAT_ID if target == "approval" else None
    if request.args.get("photo"):
        import base64
        import tempfile
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(png_bytes)
            tmp_path = tmp.name
        try:
            status = send_telegram_photo(
                tmp_path, caption=f"🔧 Тестовое фото с сайта ({target})", chat_id=chat_id,
            )
        finally:
            os.remove(tmp_path)
        return f"telegram photo status ({target}): {status}", 200
    status = send_telegram_notification(f"🔧 Тестовое уведомление с сайта ({target})", chat_id=chat_id)
    return f"telegram status ({target}): {status}", 200


@app.route("/internal/telegram-updates")
def telegram_updates():
    """Legacy diagnostic view of recent bot conversations.

    Employee links are now managed through the authenticated /employees
    interface; this token-protected endpoint remains useful for diagnosing
    Telegram delivery without exposing it in the administrator workflow.
    """
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    if not TELEGRAM_BOT_TOKEN:
        return "TELEGRAM_BOT_TOKEN not set", 200
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
            timeout=10,
        )
    except requests.RequestException as e:
        return f"error: {e}", 502
    if not resp.ok:
        return f"failed: {resp.status_code} {resp.text[:300]}", 502
    updates = resp.json().get("result", [])
    seen = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is None:
            continue
        seen[chat["id"]] = {
            "chat_id": chat["id"],
            "username": chat.get("username"),
            "name": " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])),
            "last_text": msg.get("text"),
        }
    if not seen:
        return (
            "No updates found. Have the person send any message (e.g. /start) "
            "to the bot, then reload this page.",
            200,
        )
    lines = [f"{v['chat_id']}\tusername=@{v['username']}\tname={v['name']}\tlast=\"{v['last_text']}\"" for v in seen.values()]
    return "\n".join(lines), 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/internal/diploma-debug")
def diploma_debug():
    """Visit this URL (with the right token) to see exactly what the app
    finds in static/diplomas/ — a diploma not showing up is almost always
    a filename/extension mismatch (has to be <team username>.jpg/.jpeg/
    .png/.webp, matched case-sensitively), and this shows it directly
    instead of guessing blind. Add &username=<team login> to check one
    specific captain's file."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    diplomas_dir = os.path.join(app.static_folder, "diplomas")
    try:
        files = sorted(os.listdir(diplomas_dir))
    except OSError as e:
        return f"error listing {diplomas_dir}: {e}", 200
    lines = [f"diplomas dir: {diplomas_dir}", f"files found: {files}"]
    username = request.args.get("username", "").strip()
    if username:
        lines.append(f"expected filename: {username}.jpg / .jpeg / .png / .webp")
        lines.append(f"find_diploma_url({username!r}) = {find_diploma_url(username)!r}")
    return "\n".join(lines), 200


@app.route("/internal/push-test")
def push_test():
    """Visit this URL (with the right token) to send a real test push to
    every subscribed admin browser and see exactly what happened — same
    idea as /internal/telegram-test, for the same reason: push has several
    moving parts (VAPID keys, browser permission, subscription rows), and
    guessing which one is broken from a "nothing happened" report is slow."""
    if not CRON_SECRET or request.args.get("token") != CRON_SECRET:
        return "forbidden", 403
    db = get_db()
    count = db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE role = 'admin'").fetchone()[0]
    status = send_push_notification(
        "🔧 Тестовый пуш с сайта", "Если вы это видите, всё настроено верно.", url="/admin",
    )
    return f"push status: {status} (admin subscriptions on file: {count})", 200


def _notify_offline_fleet_events(events):
    """Send the same alerts as online captain forms, once after replay."""
    db = get_db()
    for event in events:
        boat = event["boat"]
        employee_name = event["employee_name"]
        boat_index = next(
            (index for index, item in enumerate(BOATS) if item["name"] == boat),
            None,
        )
        if event["kind"] == "checklist_problem":
            checklist_label = CHECKLIST_TYPE_LABELS.get(
                event["checklist_type"], event["checklist_type"]
            )
            comment = event.get("comment") or ""
            telegram_dispatch = dispatch_notification(
                db,
                EVENT_FLEET_CHECKLIST_PROBLEM,
                f"⚠️ <b>{html.escape(checklist_label)}</b> — {html.escape(boat)}\n"
                f"Капитан: {html.escape(employee_name)}\n"
                f"Пункт: {html.escape(event['question'])}\n"
                f"Комментарий: {html.escape(comment) if comment else '—'}",
                send_telegram_notification,
            )
            push_title = f"{checklist_label} — {boat}"
            push_body = event["question"] + (f": {comment}" if comment else "")
        else:
            description = event["description"]
            if event["kind"] == "extra_defect":
                checklist_label = CHECKLIST_TYPE_LABELS.get(
                    event["checklist_type"], event["checklist_type"]
                )
                telegram_dispatch = dispatch_notification(
                    db,
                    EVENT_FLEET_EXTRA_DEFECT,
                    f"⚠️ <b>Неисправность вне чек-листа</b> — {html.escape(boat)}\n"
                    f"Капитан: {html.escape(employee_name)}\n"
                    f"Осмотр: {html.escape(checklist_label)}\n"
                    f"Описание: {html.escape(description)}",
                    send_telegram_notification,
                )
            else:
                telegram_dispatch = dispatch_notification(
                    db,
                    EVENT_FLEET_DEFECT_CREATED,
                    f"⚠️ <b>Неисправность добавлена офлайн</b> — {html.escape(boat)}\n"
                    f"Капитан: {html.escape(employee_name)}\n"
                    f"Описание: {html.escape(description)}",
                    send_telegram_notification,
                )
            push_title = f"Новая неисправность — {boat}"
            push_body = description

        dispatch_photos(
            telegram_dispatch,
            event.get("photo_paths") or [],
            send_telegram_photo,
        )
        send_push_notification(
            push_title,
            push_body,
            url=(
                f"/fleet/{boat_index}/defects/{event['record_id']}"
                if boat_index is not None
                else "/fleet"
            ),
        )


app.register_blueprint(
    create_offline_blueprint(
        get_db=get_db,
        team_login_required=team_login_required,
        employee_has_position=_employee_has_position,
        checklist_questions_for=_checklist_questions_for,
        notify_events=_notify_offline_fleet_events,
        allowed_photo_extensions=WORK_PHOTO_EXTENSIONS,
    )
)


# =======================================================================
# Возвраты по экскурсионным рейсам
# =======================================================================
app.register_blueprint(
    create_refunds_blueprint(
        get_db=get_db,
        admin_login_required=admin_login_required,
        yclients_records_fetcher=yclients_get_records,
        yclients_configured=yclients_configured,
        yookassa_request=_yookassa_request,
        yookassa_configured=yookassa_configured,
        receipt_vat_code=YOOKASSA_EXCURSION_VAT_CODE,
        receipt_payment_mode=YOOKASSA_EXCURSION_PAYMENT_MODE,
    )
)


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

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

# reportlab (PDF generation for "Акт выполненных работ") is imported lazily,
# inside _build_act_pdf() — it's an extra dependency on top of the site's
# core requirements, and a missing/broken install of it must not take the
# whole app down. If it's unavailable, only that one route fails.

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024  # cap uploaded photos at 15 MB

# Signs the investor-login session cookie. Set SECRET_KEY as a real
# environment variable in production so sessions survive restarts/deploys —
# without it, a fresh random key is generated each process start (safe,
# just logs everyone out on every restart/redeploy).
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workhours.db")


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


WORK_PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
BOAT_DOCUMENT_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".doc", ".docx")


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


def get_checklist_answer_photos(db, answer_id):
    """Photos attached as evidence to one checklist problem report. No
    per-photo comment (the problem's own comment already covers it) — shaped
    the same as get_work_item_photos so the same modal JS can show either."""
    rows = db.execute(
        "SELECT id, filename FROM checklist_answer_photos WHERE answer_id = ? ORDER BY id",
        (answer_id,),
    ).fetchall()
    return [
        {"id": r["id"], "url": url_for("static", filename=f"checklist_photos/{r['filename']}"),
         "comment": None}
        for r in rows
    ]


def format_money(value, decimals=0):
    """Format a number with a thin space as the thousands separator."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", " ")


app.jinja_env.filters["ru_date"] = format_ru_date
app.jinja_env.filters["money"] = format_money

# ---------------------------------------------------------------------
# Yclients — импорт рейсов. Токены НЕ храним в коде (секреты) — задайте их
# как переменные окружения на хостинге:
#   YCLIENTS_PARTNER_TOKEN, YCLIENTS_USER_TOKEN, YCLIENTS_COMPANY_ID
# Локально можно временно вписать значения прямо сюда для проверки.
# ---------------------------------------------------------------------
YCLIENTS_PARTNER_TOKEN = os.environ.get("YCLIENTS_PARTNER_TOKEN") or "rtzn97gwz5t6ape37egg"
YCLIENTS_USER_TOKEN = os.environ.get("YCLIENTS_USER_TOKEN") or "7a61e523fd03f146601add9408f69696"
YCLIENTS_COMPANY_ID = os.environ.get("YCLIENTS_COMPANY_ID") or "979343"

# ---------------------------------------------------------------------
# ЮKassa — приём онлайн-оплаты по заказам тюнинг-центра. В отличие от
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

# ---------------------------------------------------------------------
# Т-Банк — выгрузка выписки по расчётному счёту (раздел «Аналитика»).
# Как и с ЮKassa, это банковские реквизиты — никакого запасного значения в
# коде, только переменные окружения на хостинге:
#   TBANK_API_TOKEN, TBANK_ACCOUNT_NUMBER
# Без них раздел «Аналитика» просто покажет, что подключение не настроено.
# ---------------------------------------------------------------------
TBANK_API_TOKEN = os.environ.get("TBANK_API_TOKEN")
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
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_APPROVAL_CHAT_ID = os.environ.get("TELEGRAM_APPROVAL_CHAT_ID") or TELEGRAM_CHAT_ID


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


def tbank_configured():
    return bool(TBANK_API_TOKEN and TBANK_ACCOUNT_NUMBER)

# Соответствие цвета записи/события в Yclients — катеру. Значения подтверждены.
BOAT_COLORS = {
    "#03a9f4": "Ларус",             # синий
    "#2196f3": "Ларус",             # синий (второй встречающийся оттенок)
    "#673ab7": "Бодрый Второй",     # тёмно-фиолетовый
    "#8bc34a": "Бодрый Первый",     # светло-зелёный
}

# Красная "запись-блокер": менеджер ставит её сотруднику вместо реального
# рейса, когда его точно нельзя занимать в этот день (комментарий обычно
# "не ставить в рейсы"). Это не рейс и не смена — такую запись нужно
# полностью игнорировать: не создавать под неё карточку на подтверждение и
# не считать её поводом для доплаты за смену.
BLOCKED_SHIFT_COLOR = "f44336"

# ---------------------------------------------------------------------
# СПРАВОЧНИКИ — отредактируйте под себя.
# ---------------------------------------------------------------------

# Список сотрудников для выпадающего списка.
EMPLOYEES = [
    "Даниил Галецкий",
    "Дмитрий Тарусов",
    "Кирилл Бурнасов",
    "Эльмира Бектаева",
    "Платон Жмаев",
    "Михаил Вишневский",
    "Андрей Жаворонков",
    "Арсений Коннов",
    "Марина Кащенко",
    "Юрий Мороз",
    "Игорь Севостьянов",
    "Алексей Чабанов",
    "Андрей Краснюков",
]

# Должности сотрудников. У одного человека может быть несколько должностей —
# указывайте их списком. Заполняется по мере согласования; имя, не попавшее
# сюда, просто останется без должностей в базе, ничего не сломается.
EMPLOYEE_POSITIONS = {
    # "Имя Фамилия": ["Должность 1", "Должность 2"],
    "Даниил Галецкий": ["Тюнингмэн", "Гид-капитан", "Капитан"],
    "Эльмира Бектаева": "Гид",
    "Дмитрий Тарусов": ["Тюнингмэн", "Капитан"],
    "Алексей Чабанов": "Тюнингмэн",
    "Андрей Краснюков": "Тюнингмэн",
    "Андрей Жаворонков": ["Тюнингмэн", "Капитан"],
    "Арсений Коннов": "Гид",
    "Платон Жмаев": ["Капитан", "Гид-капитан", "Тюнингмэн"],
    "Кирилл Бурнасов": "Гид",
    "Юрий Мороз": "Тюнингмэн",
    "Михаил Вишневский": "Капитан",
    "Марина Кащенко": "Менеджер по работе с клиентами",
    "Игорь Севостьянов": ["Капитан", "Тюнингмэн"],
}

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

# Катера: инвестор, комиссия управляющего (%) в зависимости от канала продажи,
# и стандартные (умолчательные) суммы топлива/стоянки за рейс — всё это можно
# поправить вручную прямо в форме при добавлении рейса.
# ПРЕДПОЛОЖЕНИЕ: у "Бодрый Второй" тот же инвестор, что и у "Ларус"
# (Владимир Леонтьев) — поправьте здесь, если это не так.
BOATS = [
    {
        "name": "Ларус",
        "investor": "Владимир Леонтьев",
        "commission_direct": 30,
        "commission_aggregator": 30,
        "fuel": 768,
        "mooring": 1333,
    },
    {
        "name": "Бодрый Второй",
        "investor": "Владимир Леонтьев",
        "commission_direct": 30,
        "commission_aggregator": 30,
        "fuel": 768,
        "mooring": 1333,
    },
    {
        "name": "Бодрый Первый",
        "investor": "Андрей Жаворонков",
        "commission_direct": 30,
        "commission_aggregator": 39,
        "fuel": 768,
        "mooring": 1333,
    },
]

# Чек-листы осмотра лодки, которые капитан проходит из личного кабинета —
# по одному вопросу за раз, как онлайн-тест. У каждого типа осмотра есть
# "common" — общий список вопросов, одинаковый для всех катеров, и
# "by_boat" — вопросы, специфичные для конкретного катера (по имени, как
# в BOATS), которые добавляются к общим. Чек-лист для катера = common +
# by_boat[катер]. Чтобы добавить вопросы под конкретный катер, впишите их
# в by_boat, например: "Ларус": ["Проверить трюмный насос", ...].
CHECKLIST_TYPE_LABELS = {
    "pre": "Предрейсовый осмотр",
    "post": "Послерейсовый осмотр",
}
CHECKLIST_QUESTIONS = {
    "pre": {
        "common": [
            "Проверьте положение резинового кольца в стакане сепаратора, плавает ли оно? "
            "Если нет — всё в порядке, если да — слейте воду с помощью краника внизу, "
            "нажмите на кнопку «Проблема» и сообщите в комментарии про воду в топливе",
            "Не поднимая мотор, снимите колпак, найдите жёлтую ручку масляного щупа — "
            "достаньте, оботрите ветошью, вставьте обратно. После этого снова достаньте — "
            "уровень масла должен быть между двумя отметками на щупе. Если это так — жмите "
            "«Всё в порядке», если нет — жмите «Проблема» и опишите уровень масла в комментарии",
            "Внимательно осмотрите крепления тросов газа и реверса: все наконечники должны быть "
            "зашплинтованы! Если это не так — наши соболезнования... Нажмите кнопку «Проблема» — "
            "вдруг вам станет легче от этого?",
            "Поднимите мотор и осмотрите гребной винт: он должен быть без следов повреждений, "
            "на нём не должно быть посторонних объектов (водорослей, верёвок и т. д.). "
            "Фиксирующая гайка должна быть плотно затянута и зашплинтована. Если обнаружили "
            "дефект — смело жмите «Проблема»",
            "Неужели всё ещё всё в порядке?? Ну тогда рискнём завести мотор! Убедись, что мотор "
            "опущен, масса включена, чека вставлена — и заводи! Обрати внимание на обороты: "
            "держатся ли они стабильно? Нет? Ничего страшного — сейчас всё немного нестабильно, "
            "но кнопку «Проблема» нажать в этом случае всё же стоит. Обрати внимание на посторонние "
            "звуки, излишнее задымление, а также на ошибки, загоревшиеся на многофункциональном "
            "приборе — если что-то из этого есть, смело жми «Проблема»",
            "Раз уж вы всё ещё не заглушили мотор, давайте проверим ещё кое-что: аккуратно "
            "включите переднюю передачу — идёт ли катер вперёд? Потом попробуйте заднюю — идёт "
            "назад? Если да, то есть шансы, что ваша смена пройдёт нормально! Если нет — вы знаете, "
            "что делать: жмите «Проблема»",
            "С мотором покончено (фигурально). Теперь проверим электрику — ходовые огни, "
            "топовый огонь, звуковой сигнал (если есть), всё должно работать. Если что-то вдруг "
            "не включается — жмите кнопку «Проблема»",
            "Ну дальше вообще мелочи: проверь, топлива-то тебе хватит? Если нет — нажми "
            "«Проблема» чисто для соблюдения формальности, а вот как быть с топливом — не знаю…",
            "Ты почти готов к походу! Ну а если у тебя турист ногу сломает и в воду упадёт? А?? "
            "Проверь комплектность спасательного оборудования (прежде всего жилеты, круги и "
            "аптечку). Если чего-то не хватает, жми на кнопку «Проблема» — это будет весомое "
            "оправдание перед пострадавшим туристом!",
            "Ну и последнее — обязательно проверь швартово-такелажное хозяйство: швартовов "
            "должно быть нужное количество, кранцы должны быть не сдуты, протектор на шине не "
            "менее 1.6 мм (иначе гаишники оштрафуют). Если нашёл проблему — жми на уже знакомую "
            "кнопку.",
        ],
        "by_boat": {
            # "Ларус": ["Вопрос, специфичный только для Ларуса"],
        },
    },
    "post": {
        "common": [
            "Двигатель заглушен, приборы отключены",
            "Уровень топлива и расход зафиксированы",
            "Корпус осмотрен на предмет новых повреждений",
            "Мусор и личные вещи пассажиров убраны с борта",
            "Спасательные жилеты собраны и убраны на место",
            "Швартовка выполнена, судно надёжно закреплено",
            "Палуба вымыта, лодка готова к следующему рейсу",
        ],
        "by_boat": {
        },
    },
}


def _checklist_questions_for(checklist_type, boat):
    section = CHECKLIST_QUESTIONS.get(checklist_type) or {}
    return list(section.get("common", [])) + list(section.get("by_boat", {}).get(boat, []))

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

# Личный кабинет члена команды: (имя — должно совпадать с EMPLOYEES и с
# полем employee в entries, логин, хеш пароля). Хеш добавляйте через
# werkzeug.security.generate_password_hash(pwd, method="pbkdf2:sha256") —
# сам пароль в коде никогда не хранится. Добавляйте сюда новых сотрудников
# по мере необходимости — при следующем запуске аккаунт создастся сам.
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
]

SALE_CHANNELS = [
    {"value": "direct", "label": "Напрямую"},
    {"value": "aggregator", "label": "Через агрегатора/агента"},
    {"value": "mixed", "label": "Смешанно / другое (укажу комиссию сам)"},
]

ORDER_STATUSES = [
    {"value": "estimate", "label": "Предварительный расчёт"},
    {"value": "in_progress", "label": "В работе"},
    {"value": "qc", "label": "Проходит независимый контроль качества"},
    {"value": "done", "label": "Выполнен"},
    {"value": "cancelled", "label": "Отменён"},
]
DEFAULT_ORDER_STATUS = "estimate"

WORK_STATUSES = [
    {"value": "pending", "label": "На согласовании"},
    {"value": "approved", "label": "Согласовано"},
    {"value": "in_progress", "label": "В работе"},
    {"value": "done", "label": "Выполнено"},
]
DEFAULT_WORK_STATUS = "pending"


def order_status_label(value):
    for s in ORDER_STATUSES:
        if s["value"] == value:
            return s["label"]
    return value


def work_status_label(value):
    for s in WORK_STATUSES:
        if s["value"] == value:
            return s["label"]
    return value


app.jinja_env.filters["order_status_label"] = order_status_label
app.jinja_env.filters["work_status_label"] = work_status_label

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
            created_at TEXT NOT NULL
        )
        """
    )
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
    # EMPLOYEE_POSITIONS is the source of truth — resync employee_positions
    # to match it on every restart (add what's missing, drop what's no
    # longer listed), so editing the dict and redeploying is all it takes.
    for name, positions in EMPLOYEE_POSITIONS.items():
        if isinstance(positions, str):
            positions = [positions]
        employee_row = conn.execute(
            "SELECT id FROM employees WHERE name = ?", (name,)
        ).fetchone()
        if employee_row is None:
            continue
        employee_id = employee_row[0]
        current_positions = {
            r[0] for r in conn.execute(
                "SELECT position FROM employee_positions WHERE employee_id = ?",
                (employee_id,),
            ).fetchall()
        }
        for position in positions:
            if position not in current_positions:
                conn.execute(
                    "INSERT OR IGNORE INTO employee_positions (employee_id, position, created_at) "
                    "VALUES (?, ?, ?)",
                    (employee_id, position, now_str),
                )
        for position in current_positions - set(positions):
            conn.execute(
                "DELETE FROM employee_positions WHERE employee_id = ? AND position = ?",
                (employee_id, position),
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
            boat_model TEXT NOT NULL,
            sale_channel TEXT NOT NULL,
            phone TEXT NOT NULL,
            discount_pct REAL NOT NULL DEFAULT 0,
            subtotal REAL NOT NULL,
            total REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'estimate',
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
        CREATE TABLE IF NOT EXISTS tuning_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            paid_at TEXT NOT NULL,
            created_at TEXT NOT NULL
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
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT NOT NULL,
            boat_model TEXT NOT NULL,
            phone TEXT NOT NULL UNIQUE,
            token TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        )
        """
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
            employee_name TEXT NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
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
        conn.execute(
            "INSERT OR IGNORE INTO team_accounts (employee_name, username, password_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (employee_name, username, password_hash, now),
        )
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
    if selected_week != "all":
        paid_employees = {
            row["employee"] for row in db.execute(
                "SELECT employee FROM payments WHERE period_key = ?", (selected_week,)
            ).fetchall()
        }

    # Employees for the filter dropdown: the configured list, plus any
    # employee names already used but not in the list (so nothing is hidden).
    known = list(EMPLOYEES)
    for row in db.execute("SELECT DISTINCT employee FROM entries").fetchall():
        if row["employee"] not in known:
            known.append(row["employee"])

    projects = db.execute(
        "SELECT projects.*, tuning_orders.client_name AS client_name, "
        "tuning_orders.boat_model AS boat_model "
        "FROM projects LEFT JOIN tuning_orders ON tuning_orders.id = projects.tuning_order_id "
        "ORDER BY projects.created_at DESC, projects.id DESC"
    ).fetchall()

    return dict(
        entries=entries,
        totals_by_employee=totals_by_employee,
        grand_total=grand_total,
        weeks=weeks,
        selected_week=selected_week,
        employees_filter=known,
        selected_employee=selected_employee,
        paid_employees=paid_employees,
        projects=projects,
    )


@app.route("/")
def home():
    return render_template("home.html")


def admin_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
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
        employees_form=EMPLOYEES,
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
            employees_form=EMPLOYEES,
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
            employees_form=EMPLOYEES,
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
    )


def _trips_common_kwargs():
    return dict(
        employees_form=EMPLOYEES,
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
        "trips.html", **ctx, **_trips_common_kwargs(), edit_trip=None,
        trip_expense_error=session.pop("trip_expense_error", None),
    )


@app.route("/trips/add", methods=["POST"])
@admin_login_required
def add_trip():
    db = get_db()
    errors, data = _process_trip_form(db, request.form)
    if errors:
        ctx = _trips_list_context(db)
        return render_template(
            "trips.html", **ctx, **_trips_common_kwargs(),
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
            "trips.html", **ctx, **_trips_common_kwargs(),
            edit_trip=trip, form_values=form_values,
            labor_prefill=labor_prefill,
            expenses_prefill=[(e["description"], e["amount"]) for e in exps],
        )

    errors, data = _process_trip_form(db, request.form, exclude_trip_id=trip_id)
    if errors:
        ctx = _trips_list_context(db)
        return render_template(
            "trips.html", **ctx, **_trips_common_kwargs(),
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


@app.route("/trips/delete/<int:trip_id>", methods=["POST"])
@admin_login_required
def delete_trip(trip_id):
    db = get_db()
    trip = db.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if trip is not None:
        labor_links = db.execute(
            "SELECT entry_id FROM trip_labor WHERE trip_id = ?", (trip_id,)
        ).fetchall()
        entry_ids = {l["entry_id"] for l in labor_links}
        if trip["entry_id"]:
            entry_ids.add(trip["entry_id"])
        for eid in entry_ids:
            db.execute("DELETE FROM entries WHERE id = ?", (eid,))
        db.execute("DELETE FROM trip_labor WHERE trip_id = ?", (trip_id,))
        db.execute("DELETE FROM trip_expenses WHERE trip_id = ?", (trip_id,))
        db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
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
    if employee and employee not in EMPLOYEES:
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


# =======================================================================
# Флот
# =======================================================================

def _fleet_boat_checklists(db, boat):
    """All inspection checklists for one boat, newest first, each annotated
    with its question count and problem list (with photos) — same shape the
    captain's own checklist-run view uses, so the same template partial
    could render either."""
    rows = db.execute(
        "SELECT * FROM boat_checklists WHERE boat = ? ORDER BY started_at DESC, id DESC",
        (boat,),
    ).fetchall()
    checklists = []
    for row in rows:
        questions = _checklist_questions_for(row["checklist_type"], row["boat"])
        answers = db.execute(
            "SELECT * FROM boat_checklist_answers WHERE checklist_id = ? ORDER BY question_index",
            (row["id"],),
        ).fetchall()
        problems = [
            {"question_text": a["question_text"], "comment": a["comment"],
             "photos": get_checklist_answer_photos(db, a["id"])}
            for a in answers if a["status"] == "problem"
        ]
        checklists.append({
            "id": row["id"], "checklist_type": row["checklist_type"],
            "employee_name": row["employee_name"], "started_at": row["started_at"],
            "completed_at": row["completed_at"], "total": len(questions),
            "answered": len(answers), "problems": problems,
        })
    return checklists


@app.route("/fleet")
@admin_login_required
def fleet_index():
    return render_template("fleet_index.html", boats=BOATS, active_page="fleet")


@app.route("/fleet/<boat>")
@admin_login_required
def fleet_boat(boat):
    if boat not in [b["name"] for b in BOATS]:
        return redirect(url_for("fleet_index"))
    db = get_db()
    checklists = _fleet_boat_checklists(db, boat)
    documents = db.execute(
        "SELECT * FROM boat_documents WHERE boat = ? ORDER BY uploaded_at DESC, id DESC",
        (boat,),
    ).fetchall()
    return render_template(
        "fleet_boat.html", boat=boat, checklists=checklists, documents=documents,
        checklist_type_labels=CHECKLIST_TYPE_LABELS, active_page="fleet",
    )


@app.route("/fleet/<boat>/documents", methods=["POST"])
@admin_login_required
def upload_boat_document(boat):
    if boat not in [b["name"] for b in BOATS]:
        return redirect(url_for("fleet_index"))
    title = request.form.get("title", "").strip()
    file = request.files.get("document")
    if title and file and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext in BOAT_DOCUMENT_EXTENSIONS:
            docs_dir = os.path.join(app.static_folder, "boat_documents")
            os.makedirs(docs_dir, exist_ok=True)
            filename = f"{secrets.token_hex(8)}{ext}"
            file.save(os.path.join(docs_dir, filename))
            db = get_db()
            db.execute(
                "INSERT INTO boat_documents (boat, title, filename, original_filename, uploaded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (boat, title, filename, file.filename, dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            db.commit()
    return redirect(url_for("fleet_boat", boat=boat))


@app.route("/fleet/<boat>/documents/<int:doc_id>")
@admin_login_required
def download_boat_document(boat, doc_id):
    db = get_db()
    doc = db.execute(
        "SELECT * FROM boat_documents WHERE id = ? AND boat = ?", (doc_id, boat)
    ).fetchone()
    if doc is None:
        return redirect(url_for("fleet_boat", boat=boat))
    docs_dir = os.path.join(app.static_folder, "boat_documents")
    return send_from_directory(
        docs_dir, doc["filename"], download_name=doc["original_filename"],
    )


@app.route("/fleet/<boat>/documents/<int:doc_id>/delete", methods=["POST"])
@admin_login_required
def delete_boat_document(boat, doc_id):
    db = get_db()
    doc = db.execute(
        "SELECT * FROM boat_documents WHERE id = ? AND boat = ?", (doc_id, boat)
    ).fetchone()
    if doc is not None:
        try:
            os.remove(os.path.join(app.static_folder, "boat_documents", doc["filename"]))
        except OSError:
            pass
        db.execute("DELETE FROM boat_documents WHERE id = ?", (doc_id,))
        db.commit()
    return redirect(url_for("fleet_boat", boat=boat))


# =======================================================================
# Тюнинг-центр
# =======================================================================

def _process_tuning_form(form):
    """Validate a tuning-center order form and compute derived amounts.
    Returns (errors, data). data is None if there are errors."""
    errors = []

    client_name = form.get("client_name", "").strip()
    boat_model = form.get("boat_model", "").strip()
    phone = form.get("phone", "").strip()
    sale_channel = form.get("sale_channel", "direct").strip()
    if sale_channel not in [c["value"] for c in SALE_CHANNELS]:
        sale_channel = "direct"

    if not client_name:
        errors.append("Укажите ФИО клиента.")
    if not boat_model:
        errors.append("Укажите модель лодки.")
    if not phone:
        errors.append("Укажите номер телефона.")

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
            items.append({"work_name": name, "cost_price": cost, "multiplier": mult, "price": price})
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
        client_name=client_name, boat_model=boat_model, phone=phone,
        sale_channel=sale_channel, discount_type=discount_type, discount_value=discount_value,
        # discount_pct is kept only for older code/rows that still read it —
        # 0 when the discount is a fixed amount, since it isn't a percent.
        discount_pct=discount_value if discount_type == "percent" else 0.0,
        items=items, subtotal=subtotal, discount_amount=discount_amount, total=total,
    )
    return errors, data


def _get_or_create_client(db, phone, client_name, boat_model):
    """Find the client cabinet for this phone number, refreshing their name
    and boat, or create one with a fresh unique link if none exists yet."""
    row = db.execute("SELECT id FROM clients WHERE phone = ?", (phone,)).fetchone()
    if row:
        db.execute(
            "UPDATE clients SET client_name = ?, boat_model = ? WHERE id = ?",
            (client_name, boat_model, row["id"]),
        )
        return row["id"]
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    token = secrets.token_urlsafe(16)
    cur = db.execute(
        "INSERT INTO clients (client_name, boat_model, phone, token, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (client_name, boat_model, phone, token, now),
    )
    return cur.lastrowid


def _order_payment_totals(db, order_id, total):
    payments = db.execute(
        "SELECT * FROM tuning_payments WHERE order_id = ? ORDER BY paid_at DESC, id DESC",
        (order_id,),
    ).fetchall()
    paid_amount = sum(p["amount"] for p in payments)
    remaining = max(0.0, total - paid_amount)
    return payments, paid_amount, remaining


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


def _build_act_pdf(order, items):
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

    try:
        order_date = dt.date.fromisoformat(order["created_at"][:10]).strftime("%d.%m.%Y")
    except ValueError:
        order_date = order["created_at"][:10]

    flow = []
    logo_path = os.path.join(app.static_folder, "logo-act.png")
    logo_w = 130
    flow.append(Image(logo_path, width=logo_w, height=logo_w * 230 / 836))
    flow.append(Spacer(1, 12))
    flow.append(Paragraph(f"<u>{COMPANY_NAME}</u>", style_company))
    flow.append(Paragraph(COMPANY_ADDRESS, style_address))
    flow.append(Paragraph("Акт выполненных работ", style_title))
    flow.append(Paragraph(f"По заказу № {order['id']} от {order_date}", style_subtitle))

    flow.append(Paragraph(f"Заказчик {order['client_name']} ({order['boat_model']})", style_client))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.black,
                            spaceBefore=2, spaceAfter=18))

    table_data = [["№", "Наименование товара", "Цена", "Кол-во", "Ед. изм.", "Сумма"]]
    total_sum = 0.0
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

    col_widths = [12 * mm, 76 * mm, 24 * mm, 18 * mm, 18 * mm, 22 * mm]
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
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
    ]))
    flow.append(tbl)
    flow.append(Spacer(1, 22))

    flow.append(Paragraph(
        f"Итого выполнено работ на сумму: {_rubles_to_words(total_sum)}", style_bold,
    ))
    flow.append(Spacer(1, 14))
    flow.append(Paragraph("Работы выполнено качественно и в срок и полностью оплачены", style_bold))
    flow.append(Paragraph("Стороны претензий друг к другу не имеют", style_bold))
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
    try:
        pdf_bytes = _build_act_pdf(order, items)
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


def _build_handover_act_pdf(order, items):
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

    try:
        order_date = dt.date.fromisoformat(order["created_at"][:10]).strftime("%d.%m.%Y")
    except ValueError:
        order_date = order["created_at"][:10]

    flow = []
    logo_path = os.path.join(app.static_folder, "logo-act.png")
    logo_w = 130
    flow.append(Image(logo_path, width=logo_w, height=logo_w * 230 / 836))
    flow.append(Spacer(1, 12))
    flow.append(Paragraph(f"<u>{COMPANY_NAME}</u>", style_company))
    flow.append(Paragraph(COMPANY_ADDRESS, style_address))
    flow.append(Paragraph("Акт приёма-передачи", style_title))
    flow.append(Paragraph(f"По заказу № {order['id']} от {order_date}", style_subtitle))

    flow.append(Paragraph(f"Заказчик {order['client_name']} ({order['boat_model']})", style_client))
    flow.append(HRFlowable(width="100%", thickness=0.6, color=colors.black,
                            spaceBefore=2, spaceAfter=18))

    table_data = [["№", "Наименование товара", "Цена", "Кол-во", "Ед. изм.", "Сумма"]]
    total_sum = 0.0
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

    if order["discount_type"] == "amount":
        discount_amount = order["discount_value"]
    else:
        discount_amount = total_sum * order["discount_value"] / 100
    summary_rows = 1
    if discount_amount > 0:
        discount_label = (
            f"Скидка ({('%g' % order['discount_value']).replace('.', ',')}%):"
            if order["discount_type"] != "amount"
            else "Скидка:"
        )
        table_data.append([
            "", "", "", "", discount_label,
            f"{discount_amount:.2f}".replace(".", ","),
        ])
        table_data.append([
            "", "", "", "", "К оплате:",
            f"{total_sum - discount_amount:.2f}".replace(".", ","),
        ])
        summary_rows = 3

    col_widths = [12 * mm, 76 * mm, 24 * mm, 18 * mm, 18 * mm, 22 * mm]
    tbl = Table(table_data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "OpenSans"),
        ("FONTNAME", (0, 0), (-1, 0), "OpenSans-Bold"),
        ("FONTNAME", (0, -summary_rows), (-1, -1), "OpenSans-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (2, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    flow.append(tbl)
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


@app.route("/tuning/<int:order_id>/handover.pdf")
@admin_login_required
def tuning_order_handover_pdf(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))
    items = db.execute(
        "SELECT * FROM tuning_order_items WHERE order_id = ? ORDER BY id", (order_id,)
    ).fetchall()
    try:
        pdf_bytes = _build_handover_act_pdf(order, items)
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
    orders = db.execute(
        "SELECT o.*, c.token AS client_token FROM tuning_orders o "
        "LEFT JOIN clients c ON c.id = o.client_id "
        "ORDER BY o.created_at DESC, o.id DESC"
    ).fetchall()
    grand_total = sum(o["total"] for o in orders)
    return render_template(
        "tuning_index.html", orders=orders, grand_total=grand_total,
        active_page="tuning", sub_page="orders",
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
        return render_template(
            "tuning_form.html", edit_order=None, errors=None, form_values=None,
            items_prefill=None, sale_channels=SALE_CHANNELS, active_page="tuning", sub_page="orders",
        )

    db = get_db()
    errors, data = _process_tuning_form(request.form)
    if errors:
        return render_template(
            "tuning_form.html", edit_order=None, errors=errors, form_values=request.form,
            items_prefill=None, sale_channels=SALE_CHANNELS, active_page="tuning", sub_page="orders",
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    client_id = _get_or_create_client(db, data["phone"], data["client_name"], data["boat_model"])
    cur = db.execute(
        "INSERT INTO tuning_orders (client_id, client_name, boat_model, sale_channel, phone, "
        "discount_pct, discount_type, discount_value, subtotal, total, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (client_id, data["client_name"], data["boat_model"], data["sale_channel"], data["phone"],
         data["discount_pct"], data["discount_type"], data["discount_value"],
         data["subtotal"], data["total"], DEFAULT_ORDER_STATUS, now, now),
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
    db.commit()
    return redirect(url_for("tuning_index"))


@app.route("/tuning/edit/<int:order_id>", methods=["GET", "POST"])
@admin_login_required
def edit_tuning_order(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM tuning_orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        return redirect(url_for("tuning_index"))

    if request.method == "GET":
        items = db.execute(
            "SELECT * FROM tuning_order_items WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
        payments, paid_amount, remaining = _order_payment_totals(db, order_id, order["total"])
        yookassa_payments = db.execute(
            "SELECT * FROM tuning_yookassa_payments WHERE order_id = ? ORDER BY id DESC", (order_id,)
        ).fetchall()
        form_values = {
            "client_name": order["client_name"], "boat_model": order["boat_model"],
            "sale_channel": order["sale_channel"], "phone": order["phone"],
            "discount_type": order["discount_type"], "discount_value": order["discount_value"],
        }
        hull_sheets = db.execute(
            "SELECT * FROM hull_diagnostic_sheets WHERE tuning_order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
        available_hull_sheets = db.execute(
            "SELECT * FROM hull_diagnostic_sheets WHERE tuning_order_id IS NULL ORDER BY boat_name"
        ).fetchall()
        work_photos_by_item = {item["id"]: get_work_item_photos(db, item["id"]) for item in items}
        return render_template(
            "tuning_form.html", edit_order=order, errors=None, form_values=form_values,
            items_prefill=items, sale_channels=SALE_CHANNELS, active_page="tuning", sub_page="orders",
            payments=payments, paid_amount=paid_amount, remaining=remaining,
            order_statuses=ORDER_STATUSES, work_statuses=WORK_STATUSES,
            yookassa_payments=yookassa_payments, yookassa_configured=yookassa_configured(),
            yookassa_error=session.pop("yookassa_error", None),
            hull_sheets=hull_sheets, available_hull_sheets=available_hull_sheets,
            work_photos_by_item=work_photos_by_item,
        )

    errors, data = _process_tuning_form(request.form)
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
            payments=payments, paid_amount=paid_amount, remaining=remaining,
            order_statuses=ORDER_STATUSES, work_statuses=WORK_STATUSES,
            yookassa_payments=yookassa_payments, yookassa_configured=yookassa_configured(),
            yookassa_error=None,
            hull_sheets=hull_sheets, available_hull_sheets=available_hull_sheets,
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    client_id = _get_or_create_client(db, data["phone"], data["client_name"], data["boat_model"])
    db.execute(
        "UPDATE tuning_orders SET client_id=?, client_name=?, boat_model=?, sale_channel=?, phone=?, "
        "discount_pct=?, discount_type=?, discount_value=?, subtotal=?, total=?, updated_at=? WHERE id=?",
        (client_id, data["client_name"], data["boat_model"], data["sale_channel"], data["phone"],
         data["discount_pct"], data["discount_type"], data["discount_value"],
         data["subtotal"], data["total"], now, order_id),
    )
    # Preserve each surviving work row's status by position — the form
    # resubmits every row on every save (even ones untouched here), so a
    # plain delete+recreate would silently reset progress on every edit.
    old_statuses = [
        r["status"] for r in db.execute(
            "SELECT status FROM tuning_order_items WHERE order_id = ? ORDER BY id", (order_id,)
        ).fetchall()
    ]
    db.execute("DELETE FROM tuning_order_items WHERE order_id = ?", (order_id,))
    for i, item in enumerate(data["items"]):
        status = old_statuses[i] if i < len(old_statuses) else DEFAULT_WORK_STATUS
        db.execute(
            "INSERT INTO tuning_order_items (order_id, work_name, cost_price, multiplier, price, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, item["work_name"], item["cost_price"], item["multiplier"], item["price"], status),
        )
    db.commit()
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
    try:
        amount = float(amount_raw)
    except ValueError:
        amount = None
    if amount is not None and amount > 0:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at) VALUES (?, ?, ?, ?)",
            (order_id, amount, now, now),
        )
        db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/tuning/<int:order_id>/pay/<int:payment_id>/delete", methods=["POST"])
@admin_login_required
def delete_tuning_payment(order_id, payment_id):
    db = get_db()
    db.execute("DELETE FROM tuning_payments WHERE id = ? AND order_id = ?", (payment_id, order_id))
    db.commit()
    return redirect(url_for("edit_tuning_order", order_id=order_id))


@app.route("/client/<token>")
def client_dashboard(token):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE token = ?", (token,)).fetchone()
    if client is None:
        return redirect(url_for("home"))
    order_rows = db.execute(
        "SELECT * FROM tuning_orders WHERE client_id = ? ORDER BY created_at DESC, id DESC",
        (client["id"],),
    ).fetchall()

    orders = []
    paid_total = 0.0
    remaining_total = 0.0
    for o in order_rows:
        _, paid_amount, remaining = _order_payment_totals(db, o["id"], o["total"])
        items = db.execute(
            "SELECT id, work_name, price, status FROM tuning_order_items "
            "WHERE order_id = ? ORDER BY id",
            (o["id"],),
        ).fetchall()
        order = dict(o)
        order["paid_amount"] = paid_amount
        order["remaining"] = remaining
        order["work_items"] = items
        order["hull_sheets"] = db.execute(
            "SELECT * FROM hull_diagnostic_sheets WHERE tuning_order_id = ? ORDER BY id", (o["id"],)
        ).fetchall()
        orders.append(order)
        paid_total += paid_amount
        remaining_total += remaining

    grand_total = sum(o["total"] for o in order_rows)

    # Attach each order's most recent unpaid ЮKassa payment link, if any,
    # so the client can pay online straight from their cabinet.
    for order in orders:
        pending = db.execute(
            "SELECT * FROM tuning_yookassa_payments WHERE order_id = ? AND status != 'succeeded' "
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
        work_photos_by_item=work_photos_by_item,
    )


@app.route("/client/<token>/item/<int:item_id>/approve", methods=["POST"])
def client_approve_item(token, item_id):
    db = get_db()
    client = db.execute("SELECT * FROM clients WHERE token = ?", (token,)).fetchone()
    if client is None:
        return redirect(url_for("home"))
    item = db.execute(
        "SELECT toi.id, toi.status, toi.work_name, o.client_name, o.boat_model "
        "FROM tuning_order_items toi "
        "JOIN tuning_orders o ON o.id = toi.order_id "
        "WHERE toi.id = ? AND o.client_id = ?",
        (item_id, client["id"]),
    ).fetchone()
    if item is not None and item["status"] == "pending":
        db.execute("UPDATE tuning_order_items SET status = 'approved' WHERE id = ?", (item_id,))
        db.commit()
        send_telegram_notification(
            f"✅ Клиент согласовал работу\n"
            f"Клиент: {html.escape(item['client_name'])} ({html.escape(item['boat_model'])})\n"
            f"Работа: {html.escape(item['work_name'])}",
            chat_id=TELEGRAM_APPROVAL_CHAT_ID,
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


def _yookassa_request(method, path, json_body=None, idempotence_key=None):
    headers = {}
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    resp = requests.request(
        method, f"{YOOKASSA_API_BASE}{path}",
        auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
        json=json_body, headers=headers, timeout=15,
    )
    if not resp.ok:
        raise RuntimeError(f"ЮKassa вернула {resp.status_code}: {resp.text[:500]}")
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
            "INSERT INTO tuning_payments (order_id, amount, paid_at, created_at) VALUES (?, ?, ?, ?)",
            (record["order_id"], record["amount"], now, now),
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


@app.route("/yookassa/webhook", methods=["POST"])
def yookassa_webhook():
    # No auth — ЮKassa calls this directly. We never trust the notification
    # body for the actual status: we re-fetch the payment from the API by
    # its id before recording anything, per ЮKassa's own recommendation.
    try:
        payload = request.get_json(force=True, silent=True) or {}
        payment_id = (payload.get("object") or {}).get("id")
        if payment_id:
            db = get_db()
            record = db.execute(
                "SELECT * FROM tuning_yookassa_payments WHERE yookassa_payment_id = ?",
                (payment_id,),
            ).fetchone()
            if record is not None:
                _sync_yookassa_payment(db, record)
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
            color_raw = activity_colors.get(activity_id_raw)
            if color_raw is None and activity_id_raw.isdigit():
                color_raw = activity_colors.get(int(activity_id_raw))
            color = _normalize_color(color_raw)
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

        if raw_color_seen == BLOCKED_SHIFT_COLOR:
            # A manager's "не ставить в рейсы" marker, not a trip — skip it
            # entirely rather than surfacing it as a candidate needing review.
            continue

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


def apply_minimum_shift_rate(db, records):
    """Every crew member has a guaranteed minimum of MIN_SHIFT_RATE per
    shift. "Staffed" comes straight from the raw Yclients records — any
    non-deleted record with a staff name counts, regardless of whether it
    ever turns into a confirmed trip — compared against what they actually
    earned that day per our own `entries` (from any source: manual entry,
    a confirmed trip, or an earlier top-up). Shortfalls get a top-up entry.

    Self-correcting: run again after trips for that day change and an
    existing top-up shrinks, grows, or disappears to match — it never just
    accumulates. Returns how many top-up entries were added/changed/removed.
    """
    staffed_days = set()
    for r in records:
        if r.get("deleted") or _yclients_record_is_blocker(r):
            continue
        name = (r.get("staff") or {}).get("name", "").strip()
        if not name:
            continue
        staffed_days.add((name, _yclients_record_date(r)))

    changed = 0
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

    # A boat-less candidate can still be matched up by date+time alone, as
    # long as exactly one resolved-boat candidate shares that exact slot —
    # if two+ boats have trips at the same moment, don't guess which one
    # this partner belongs to; leave it for manual review instead.
    slots_by_datetime = {}
    for slot in groups:
        boat, trip_date, trip_time = slot
        slots_by_datetime.setdefault((trip_date, trip_time), []).append(slot)
    for row, payload in unresolved:
        when = (payload.get("trip_date", ""), payload.get("trip_time", ""))
        matches = slots_by_datetime.get(when) or []
        if len(matches) == 1:
            groups[matches[0]].append((row, payload))

    merged_away = 0
    for slot, items in groups.items():
        if len(items) < 2:
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
        "trips.html", **ctx, **_trips_common_kwargs(),
        edit_trip=None,
        import_error=request.args.get("error"),
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

    already = {
        row["yclients_ref"]
        for row in db.execute("SELECT yclients_ref FROM yclients_imports").fetchall()
    }
    existing_candidates = {
        row["yclients_ref"]
        for row in db.execute("SELECT yclients_ref FROM import_candidates").fetchall()
    }

    candidates = build_import_candidates(records, activity_colors)

    # A pending candidate's yclients_ref is built from its color + time (see
    # _yclients_group_key) — so fixing a wrongly-set color on Yclients' side
    # for a record that's already sitting in the queue produces a *new* ref
    # on the next fetch, not an update to the old one. Without this, the
    # corrected trip imports fine under its new ref while the stale old
    # ref/candidate lingers forever, unresolved, right alongside it. Prune
    # any queued candidate whose trip falls inside the period we just
    # re-fetched but whose ref didn't come back at all this time — Yclients'
    # current data no longer produces it, so it's stale. (If this was a
    # fluke — Yclients briefly omitted a still-valid record — the next
    # fetch just re-adds it; nothing is lost for good.)
    fetched_refs = {c["yclients_ref"] for c in candidates}
    for row in db.execute("SELECT id, yclients_ref, payload FROM import_candidates").fetchall():
        if row["yclients_ref"] in fetched_refs:
            continue
        trip_date = json.loads(row["payload"]).get("trip_date", "")
        if start_date <= trip_date <= end_date:
            db.execute("DELETE FROM import_candidates WHERE id = ?", (row["id"],))
    db.commit()

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    added = 0
    for c in candidates:
        if c["yclients_ref"] in already or c["yclients_ref"] in existing_candidates:
            continue
        db.execute(
            "INSERT INTO import_candidates (yclients_ref, summary, payload, created_at) "
            "VALUES (?, ?, ?, ?)",
            (c["yclients_ref"], c["summary"], json.dumps(c["payload"], ensure_ascii=False), now),
        )
        added += 1
    db.commit()
    merge_pending_candidates(db)
    db.commit()

    # Auto-confirm: candidates with a resolved boat and valid numbers go
    # straight into the trip tables, no manual review needed. Anything that
    # fails validation (most commonly an unresolved boat color) is left
    # behind in the queue below for a human to sort out.
    pending = db.execute("SELECT * FROM import_candidates ORDER BY id ASC").fetchall()
    for row in pending:
        _try_auto_import_candidate(db, row)

    # Must run after the auto-import loop above, once this fetch's trips are
    # actually in `entries` — otherwise a shift would look short and get a
    # top-up moments before its real trips land and cover it anyway.
    apply_minimum_shift_rate(db, records)

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
        "trips.html", **ctx, **_trips_common_kwargs(),
        edit_trip=None, import_candidate=row, form_values=form_values,
        labor_prefill=payload["labor_items"],
        import_note=payload.get("note", ""),
    )


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
    validation same as an empty field would in the manual form)."""
    payload = json.loads(row["payload"])
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
            "trips.html", **ctx, **_trips_common_kwargs(),
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
        "WHERE employees.name = ? AND employee_positions.position = ?",
        (employee_name, position),
    ).fetchone() is not None


def team_login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("team_id"):
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
        "SELECT * FROM team_accounts WHERE username = ?", (username,)
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
    employee_name = session.get("team_employee_name")

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
    )


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
    if not boat:
        return render_template(
            "team_checklist_start.html",
            checklist_type=checklist_type,
            checklist_label=CHECKLIST_TYPE_LABELS[checklist_type],
            boats=[b["name"] for b in BOATS],
            error="Выберите катер.",
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
        return render_template(
            "team_checklist_run.html", checklist=checklist,
            checklist_label=CHECKLIST_TYPE_LABELS.get(checklist["checklist_type"], ""),
            done=True, problems=problems, total=len(questions),
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
            (checklist_id, question_index, questions[question_index], status, comment or None, now),
        )
        # rowcount is 0 if the INSERT OR IGNORE hit the UNIQUE constraint
        # (e.g. a double submit) — lastrowid would be stale in that case,
        # so only attach photos when a row was actually just created.
        saved_photo_paths = []
        if cur.rowcount == 1:
            answer_id = cur.lastrowid
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
            send_telegram_notification(
                f"⚠️ <b>{html.escape(checklist_label)}</b> — {html.escape(checklist['boat'])}\n"
                f"Капитан: {html.escape(employee_name)}\n"
                f"Пункт: {html.escape(questions[question_index])}\n"
                f"Комментарий: {html.escape(comment) if comment else '—'}"
            )
            for photo_path in saved_photo_paths:
                send_telegram_photo(photo_path)
    return redirect(url_for("team_checklist_run", checklist_id=checklist_id))


# ---------------------------------------------------------------------
# Аналитика — финансовая аналитика по бизнесу. Первый шаг: выписка по
# расчётному счёту из Т-Банка (см. TBANK_API_TOKEN/TBANK_ACCOUNT_NUMBER
# выше). Пока подключение не настроено, раздел просто показывает заглушку.
# ---------------------------------------------------------------------
def _tbank_request(path, params):
    resp = requests.get(
        f"{TBANK_API_BASE}{path}",
        headers={"Authorization": f"Bearer {TBANK_API_TOKEN}"},
        params=params,
        timeout=30,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Т-Банк вернул ошибку {resp.status_code}: {resp.text[:500]}")
    return resp.json()


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


@app.route("/analytics")
@admin_login_required
def analytics_index():
    db = get_db()
    transactions = db.execute(
        "SELECT * FROM bank_transactions ORDER BY operation_date DESC, id DESC LIMIT 200"
    ).fetchall()
    projects = db.execute("SELECT * FROM projects ORDER BY created_at DESC, id DESC").fetchall()
    split_rows = db.execute(
        "SELECT ts.transaction_id, ts.amount, projects.name AS project_name "
        "FROM transaction_splits ts JOIN projects ON projects.id = ts.project_id "
        "ORDER BY ts.id"
    ).fetchall()
    splits_by_transaction = {}
    for s in split_rows:
        splits_by_transaction.setdefault(s["transaction_id"], []).append(
            {"project_name": s["project_name"], "amount": s["amount"]}
        )
    today = dt.date.today()
    week_ago = today - dt.timedelta(days=7)
    return render_template(
        "analytics.html", active_page="analytics", sub_page="transactions",
        transactions=transactions, projects=projects, splits_by_transaction=splits_by_transaction,
        tbank_configured=tbank_configured(),
        fetch_default_start=week_ago.isoformat(), fetch_default_end=today.isoformat(),
        fetch_error=session.pop("tbank_fetch_error", None),
        fetch_result=session.pop("tbank_fetch_result", None),
    )


@app.route("/analytics/fetch", methods=["POST"])
@admin_login_required
def analytics_fetch():
    if not tbank_configured():
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
    income = row["income"] + split_row["income"]
    expense = row["expense"] + split_row["expense"] + entries_expense
    return income, expense, income - expense


@app.route("/analytics/projects")
@admin_login_required
def analytics_projects():
    db = get_db()
    rows = db.execute(
        "SELECT projects.*, tuning_orders.client_name AS client_name, "
        "tuning_orders.boat_model AS boat_model "
        "FROM projects LEFT JOIN tuning_orders ON tuning_orders.id = projects.tuning_order_id "
        "ORDER BY projects.created_at DESC, projects.id DESC"
    ).fetchall()
    projects = []
    for p in rows:
        income, expense, profit = _project_totals(db, p["id"])
        projects.append({
            "id": p["id"], "name": p["name"], "tuning_order_id": p["tuning_order_id"],
            "client_name": p["client_name"], "boat_model": p["boat_model"],
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
    month_income = month_row["income"]
    month_expense = month_row["expense"] + month_entries_expense

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
    income, expense, profit = _project_totals(db, project_id)
    order = None
    if project["tuning_order_id"]:
        order = db.execute(
            "SELECT * FROM tuning_orders WHERE id = ?", (project["tuning_order_id"],)
        ).fetchone()
    return render_template(
        "project_detail.html", active_page="analytics", sub_page="projects",
        project=project, order=order, transactions=transactions, unattached=unattached,
        work_entries=work_entries, income=income, expense=expense, profit=profit,
    )


@app.route("/analytics/transactions/project", methods=["POST"])
@admin_login_required
def set_transaction_project():
    db = get_db()
    transaction_id = request.form.get("transaction_id", "").strip()
    project_id = request.form.get("project_id", "").strip()
    if transaction_id.isdigit():
        # A direct single-project assignment always overrides any prior split.
        db.execute(
            "DELETE FROM transaction_splits WHERE transaction_id = ?", (int(transaction_id),)
        )
        db.execute(
            "UPDATE bank_transactions SET project_id = ? WHERE id = ?",
            (int(project_id) if project_id.isdigit() else None, int(transaction_id)),
        )
        db.commit()
    next_url = request.form.get("next") or url_for("analytics_index")
    return redirect(next_url)


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
    return redirect(next_url)


def _normalize_transaction_split(db, transaction_id):
    """After a split row is removed, a single remaining row is no longer a
    split — collapse it back into a plain project_id assignment so it
    doesn't linger as a one-row split."""
    remaining = db.execute(
        "SELECT * FROM transaction_splits WHERE transaction_id = ?", (transaction_id,)
    ).fetchall()
    if len(remaining) == 1:
        db.execute(
            "UPDATE bank_transactions SET project_id = ? WHERE id = ?",
            (remaining[0]["project_id"], transaction_id),
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
        "tuning_orders.boat_model AS boat_model "
        "FROM projects LEFT JOIN tuning_orders ON tuning_orders.id = projects.tuning_order_id "
        "ORDER BY projects.created_at DESC, projects.id DESC"
    ).fetchall()

    if request.method == "GET":
        next_url = request.args.get("next") or url_for("analytics_index")
        existing = db.execute(
            "SELECT * FROM transaction_splits WHERE transaction_id = ? ORDER BY id",
            (transaction_id,),
        ).fetchall()
        splits = [
            {"project_id": str(s["project_id"]), "amount": f"{s['amount']:.2f}".rstrip("0").rstrip(".")}
            for s in existing
        ]
        return render_template(
            "transaction_split.html", active_page="analytics", sub_page="transactions",
            transaction=transaction, projects=projects, splits=splits,
            next_url=next_url, errors=None,
        )

    next_url = request.form.get("next") or url_for("analytics_index")
    project_ids = request.form.getlist("project_id[]")
    amounts = request.form.getlist("amount[]")

    display_rows = []
    for i in range(max(len(project_ids), len(amounts))):
        pid_raw = project_ids[i].strip() if i < len(project_ids) else ""
        amt_raw = amounts[i].strip() if i < len(amounts) else ""
        if not pid_raw and not amt_raw:
            continue
        display_rows.append({"project_id": pid_raw, "amount": amt_raw})

    errors = []
    parsed_rows = []
    seen_projects = set()
    for idx, r in enumerate(display_rows, start=1):
        if not r["project_id"].isdigit():
            errors.append(f"Строка {idx}: выберите проект.")
            continue
        pid = int(r["project_id"])
        if pid in seen_projects:
            errors.append(f"Строка {idx}: этот проект уже указан в другой строке — объедините суммы в одну строку.")
            continue
        try:
            amt = float(r["amount"].replace(",", "."))
        except ValueError:
            errors.append(f"Строка {idx}: сумма должна быть числом.")
            continue
        if amt <= 0:
            errors.append(f"Строка {idx}: сумма должна быть больше нуля.")
            continue
        seen_projects.add(pid)
        parsed_rows.append({"project_id": pid, "amount": amt})

    if not errors and len(parsed_rows) < 2:
        errors.append("Укажите минимум два проекта, чтобы разбить сумму — для одного проекта используйте обычный выбор проекта.")

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
            transaction=transaction, projects=projects, splits=display_rows,
            next_url=next_url, errors=errors,
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db.execute("DELETE FROM transaction_splits WHERE transaction_id = ?", (transaction_id,))
    for r in parsed_rows:
        db.execute(
            "INSERT INTO transaction_splits (transaction_id, project_id, amount, created_at) "
            "VALUES (?, ?, ?, ?)",
            (transaction_id, r["project_id"], r["amount"], now),
        )
    db.execute("UPDATE bank_transactions SET project_id = NULL WHERE id = ?", (transaction_id,))
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
        "WHERE employee_positions.position = 'Капитан'"
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


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

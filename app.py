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
import json
import sqlite3
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, g, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workhours.db")


def format_ru_date(iso_date):
    """YYYY-MM-DD -> DD/MM/YYYY for display in trip tables."""
    if not iso_date:
        return ""
    try:
        return dt.date.fromisoformat(iso_date).strftime("%d/%m/%Y")
    except ValueError:
        return iso_date


app.jinja_env.filters["ru_date"] = format_ru_date

# ---------------------------------------------------------------------
# Yclients — импорт рейсов. Токены НЕ храним в коде (секреты) — задайте их
# как переменные окружения на хостинге:
#   YCLIENTS_PARTNER_TOKEN, YCLIENTS_USER_TOKEN, YCLIENTS_COMPANY_ID
# Локально можно временно вписать значения прямо сюда для проверки.
# ---------------------------------------------------------------------
YCLIENTS_PARTNER_TOKEN = os.environ.get("YCLIENTS_PARTNER_TOKEN") or "rtzn97gwz5t6ape37egg"
YCLIENTS_USER_TOKEN = os.environ.get("YCLIENTS_USER_TOKEN") or "7a61e523fd03f146601add9408f69696"
YCLIENTS_COMPANY_ID = os.environ.get("YCLIENTS_COMPANY_ID") or "979343"

# Соответствие цвета записи/события в Yclients — катеру. Значения подтверждены.
BOAT_COLORS = {
    "#03a9f4": "Ларус",             # синий
    "#2196f3": "Ларус",             # синий (второй встречающийся оттенок)
    "#673ab7": "Бодрый Второй",     # тёмно-фиолетовый
    "#8bc34a": "Бодрый Первый",     # светло-зелёный
}

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
]

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

SALE_CHANNELS = [
    {"value": "direct", "label": "Напрямую"},
    {"value": "aggregator", "label": "Через агрегатора/агента"},
    {"value": "mixed", "label": "Смешанно / другое (укажу комиссию сам)"},
]

MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
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


@app.route("/")
def index():
    db = get_db()

    weeks, current_monday = build_week_options(db)
    selected_week = request.args.get("week", current_monday.isoformat())
    selected_employee = request.args.get("employee", "all")

    query = "SELECT * FROM entries WHERE 1=1"
    params = []

    if selected_week != "all":
        try:
            monday = dt.date.fromisoformat(selected_week)
        except ValueError:
            monday = current_monday
            selected_week = monday.isoformat()
        sunday = monday + dt.timedelta(days=6)
        query += " AND work_date BETWEEN ? AND ?"
        params += [monday.isoformat(), sunday.isoformat()]

    if selected_employee != "all":
        query += " AND employee = ?"
        params.append(selected_employee)

    query += " ORDER BY work_date DESC, id DESC"
    entries = db.execute(query, params).fetchall()

    totals_by_employee, grand_total = compute_totals(entries)

    # Employees for the filter dropdown: the configured list, plus any
    # employee names already used but not in the list (so nothing is hidden).
    known = list(EMPLOYEES)
    for row in db.execute("SELECT DISTINCT employee FROM entries").fetchall():
        if row["employee"] not in known:
            known.append(row["employee"])

    return render_template(
        "index.html",
        entries=entries,
        totals_by_employee=totals_by_employee,
        grand_total=grand_total,
        weeks=weeks,
        selected_week=selected_week,
        employees_filter=known,
        selected_employee=selected_employee,
        employees_form=EMPLOYEES,
        work_types=WORK_TYPES,
        custom_value=CUSTOM_VALUE,
        today=dt.date.today().isoformat(),
        active_page="payroll",
    )


@app.route("/add", methods=["POST"])
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
        weeks, current_monday = build_week_options(db)
        entries = db.execute(
            "SELECT * FROM entries ORDER BY work_date DESC, id DESC"
        ).fetchall()
        totals_by_employee, grand_total = compute_totals(entries)
        known = list(EMPLOYEES)
        for row in db.execute("SELECT DISTINCT employee FROM entries").fetchall():
            if row["employee"] not in known:
                known.append(row["employee"])
        return render_template(
            "index.html",
            entries=entries,
            totals_by_employee=totals_by_employee,
            grand_total=grand_total,
            weeks=weeks,
            selected_week="all",
            employees_filter=known,
            selected_employee="all",
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
        "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (employee, work_type, rate, quantity, amount, work_date,
         dt.datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    db.commit()
    return redirect(url_for("index"))


@app.route("/delete/<int:entry_id>", methods=["POST"])
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
def trips_index():
    db = get_db()
    selected_month = request.args.get("month")
    selected_boat = request.args.get("boat", "all")
    ctx = _trips_list_context(db, selected_month, selected_boat)
    return render_template("trips.html", **ctx, **_trips_common_kwargs(), edit_trip=None)


@app.route("/trips/add", methods=["POST"])
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
        summary = f"{when_label} · {boat_label} · {employees_label} · {revenue:.0f} ₽"
        candidates.append({"yclients_ref": key, "summary": summary, "payload": payload})
    candidates.sort(key=lambda c: (c["payload"]["trip_date"], c["payload"]["trip_time"]), reverse=True)
    return candidates


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

    groups = {}
    for row in rows:
        payload = json.loads(row["payload"])
        boat = payload.get("boat") or ""
        if not boat:
            continue  # never auto-merge candidates with an unresolved boat
        slot = (boat, payload.get("trip_date", ""), payload.get("trip_time", ""))
        groups.setdefault(slot, []).append((row, payload))

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

        employees_label = ", ".join(
            i["employee"] for i in keep_payload["labor_items"] if i.get("employee")
        ) or "—"
        boat, trip_date, trip_time = slot
        trip_date_label = format_ru_date(trip_date)
        when_label = f"{trip_date_label} {trip_time}".strip() if trip_time else trip_date_label
        revenue = keep_payload.get("revenue") or 0
        summary = f"{when_label} · {boat} · {employees_label} · {revenue:.0f} ₽"

        db.execute(
            "UPDATE import_candidates SET summary = ?, payload = ? WHERE id = ?",
            (summary, json.dumps(keep_payload, ensure_ascii=False), keep_row["id"]),
        )

    db.commit()
    return merged_away


@app.route("/trips/import", methods=["GET"])
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

    return redirect(url_for("import_index"))


@app.route("/trips/import/review/<int:candidate_id>", methods=["GET"])
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


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

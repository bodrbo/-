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
import sqlite3
import datetime as dt

from flask import Flask, g, redirect, render_template, request, url_for

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workhours.db")

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
]

CUSTOM_VALUE = "__custom__"  # спец-значение для пункта "Другое..." в списках

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


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

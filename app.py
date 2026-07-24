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
    )


def _trips_common_kwargs():
    return dict(
        employees_form=EMPLOYEES,
        work_types=WORK_TYPES,
        sale_channels=SALE_CHANNELS,
        custom_value=CUSTOM_VALUE,
        today=dt.date.today().isoformat(),
        active_page="trips",
    )


def _process_trip_form(form):
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

    employee = form.get("employee", "").strip()
    if employee == CUSTOM_VALUE:
        employee = form.get("employee_custom", "").strip()
    work_type = form.get("work_type", "").strip()
    if work_type == CUSTOM_VALUE:
        work_type = form.get("work_type_custom", "").strip()

    if not employee:
        errors.append("Укажите сотрудника (капитан/гид).")
    if not work_type:
        errors.append("Укажите вид рейса.")

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

    rate = parse_num("rate", "Ставка")
    quantity = parse_num("quantity", "Часы")
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

    labor_cost = rate * quantity
    commission_amount = revenue * commission_pct / 100
    direct_costs = labor_cost + fuel_cost + mooring_cost + extra_total
    remainder = revenue - commission_amount - direct_costs
    investor_payout = remainder / 2
    my_share = commission_amount + remainder / 2

    data = dict(
        boat=boat, trip_date=trip_date, employee=employee, work_type=work_type,
        rate=rate, quantity=quantity, labor_cost=labor_cost,
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
    errors, data = _process_trip_form(request.form)
    if errors:
        ctx = _trips_list_context(db)
        return render_template(
            "trips.html", **ctx, **_trips_common_kwargs(),
            edit_trip=None, errors=errors, form_values=request.form,
        ), 400

    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    cur = db.execute(
        "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (data["employee"], data["work_type"], data["rate"], data["quantity"],
         data["labor_cost"], data["trip_date"], now),
    )
    entry_id = cur.lastrowid

    cur2 = db.execute(
        "INSERT INTO trips (boat, trip_date, work_type, entry_id, revenue, sale_channel, "
        "commission_pct, commission_amount, labor_cost, fuel_cost, mooring_cost, extra_total, "
        "remainder, investor_payout, my_share, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (data["boat"], data["trip_date"], data["work_type"], entry_id, data["revenue"],
         data["sale_channel"], data["commission_pct"], data["commission_amount"],
         data["labor_cost"], data["fuel_cost"], data["mooring_cost"], data["extra_total"],
         data["remainder"], data["investor_payout"], data["my_share"], now),
    )
    trip_id = cur2.lastrowid
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
        entry = None
        if trip["entry_id"]:
            entry = db.execute(
                "SELECT * FROM entries WHERE id = ?", (trip["entry_id"],)
            ).fetchone()
        exps = db.execute(
            "SELECT * FROM trip_expenses WHERE trip_id = ?", (trip_id,)
        ).fetchall()
        form_values = {
            "boat": trip["boat"],
            "trip_date": trip["trip_date"],
            "employee": entry["employee"] if entry else "",
            "work_type": trip["work_type"],
            "quantity": entry["quantity"] if entry else "",
            "rate": entry["rate"] if entry else "",
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
            expenses_prefill=[(e["description"], e["amount"]) for e in exps],
        )

    errors, data = _process_trip_form(request.form)
    if errors:
        ctx = _trips_list_context(db)
        return render_template(
            "trips.html", **ctx, **_trips_common_kwargs(),
            edit_trip=trip, errors=errors, form_values=request.form,
        ), 400

    if trip["entry_id"]:
        db.execute(
            "UPDATE entries SET employee=?, work_type=?, rate=?, quantity=?, amount=?, work_date=? "
            "WHERE id=?",
            (data["employee"], data["work_type"], data["rate"], data["quantity"],
             data["labor_cost"], data["trip_date"], trip["entry_id"]),
        )
        entry_id = trip["entry_id"]
    else:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = db.execute(
            "INSERT INTO entries (employee, work_type, rate, quantity, amount, work_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data["employee"], data["work_type"], data["rate"], data["quantity"],
             data["labor_cost"], data["trip_date"], now),
        )
        entry_id = cur.lastrowid

    db.execute(
        "UPDATE trips SET boat=?, trip_date=?, work_type=?, entry_id=?, revenue=?, sale_channel=?, "
        "commission_pct=?, commission_amount=?, labor_cost=?, fuel_cost=?, mooring_cost=?, "
        "extra_total=?, remainder=?, investor_payout=?, my_share=? WHERE id=?",
        (data["boat"], data["trip_date"], data["work_type"], entry_id, data["revenue"],
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
        db.execute("DELETE FROM trip_expenses WHERE trip_id = ?", (trip_id,))
        if trip["entry_id"]:
            db.execute("DELETE FROM entries WHERE id = ?", (trip["entry_id"],))
        db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))
        db.commit()
    return redirect(url_for("trips_index"))


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

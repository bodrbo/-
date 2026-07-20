"""
Учёт выполненных работ сотрудников.

Небольшое веб-приложение на Flask + SQLite:
- форма для добавления записи (сотрудник, вид работы, ставка, часы/кол-во)
- таблица всех записей с возможностью удаления
- сумма по каждому сотруднику и общая сумма

Запуск локально:
    pip install -r requirements.txt
    python app.py
Приложение будет доступно на http://127.0.0.1:5000
"""

import os
import sqlite3
from datetime import datetime

from flask import Flask, g, redirect, render_template, request, url_for

app = Flask(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workhours.db")


def get_db():
    """Return a request-scoped SQLite connection."""
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
    """Create the entries table if it doesn't exist yet."""
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
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


@app.route("/")
def index():
    db = get_db()
    entries = db.execute(
        "SELECT * FROM entries ORDER BY id DESC"
    ).fetchall()

    # Sum per employee, preserving first-seen order
    totals_by_employee = {}
    for e in entries:
        totals_by_employee.setdefault(e["employee"], 0.0)
        totals_by_employee[e["employee"]] += e["amount"]

    grand_total = sum(totals_by_employee.values())

    return render_template(
        "index.html",
        entries=entries,
        totals_by_employee=totals_by_employee,
        grand_total=grand_total,
    )


@app.route("/add", methods=["POST"])
def add_entry():
    employee = request.form.get("employee", "").strip()
    work_type = request.form.get("work_type", "").strip()
    rate_raw = request.form.get("rate", "").strip().replace(",", ".")
    quantity_raw = request.form.get("quantity", "").strip().replace(",", ".")

    errors = []
    if not employee:
        errors.append("Укажите имя сотрудника.")
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

    if errors:
        db = get_db()
        entries = db.execute("SELECT * FROM entries ORDER BY id DESC").fetchall()
        totals_by_employee = {}
        for e in entries:
            totals_by_employee.setdefault(e["employee"], 0.0)
            totals_by_employee[e["employee"]] += e["amount"]
        grand_total = sum(totals_by_employee.values())
        return render_template(
            "index.html",
            entries=entries,
            totals_by_employee=totals_by_employee,
            grand_total=grand_total,
            errors=errors,
            form_values=request.form,
        ), 400

    amount = rate * quantity
    db = get_db()
    db.execute(
        "INSERT INTO entries (employee, work_type, rate, quantity, amount, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (employee, work_type, rate, quantity, amount, datetime.now().strftime("%Y-%m-%d %H:%M")),
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

#!/usr/bin/env python3
"""Reproduces "assign a tuning task for a date -> add the employee to the
shift" on a COPY of the live database and prints the real traceback of
whatever fails. The live database is never written: the script copies it to
a temp file first and points the app at the copy (Telegram sends are
stubbed out).

Usage:
    python3 scripts/diagnose_tuning_assign_shift.py <order_id> <employee name> <YYYY-MM-DD>
Example:
    python3 scripts/diagnose_tuning_assign_shift.py 31 "Андрей Краснюков" 2026-10-12
(the first work of the order is used; order_id is the number in /tuning/edit/<N>)
"""

import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def load_env_file(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(PROJECT_ROOT.parent / ".env")
load_env_file(PROJECT_ROOT / ".env")

live_db = os.environ.get("WORKHOURS_DB_PATH") or str(PROJECT_ROOT / "workhours.db")
if len(sys.argv) < 4:
    print(__doc__)
    raise SystemExit(1)
if not os.path.exists(live_db):
    print(f"Базы {live_db} нет.")
    raise SystemExit(1)
copy_dir = tempfile.mkdtemp(prefix="assign-shift-diagnose-")
copy_db = os.path.join(copy_dir, "copy.db")
shutil.copy(live_db, copy_db)
os.environ["WORKHOURS_DB_PATH"] = copy_db
print(f"Работаю на копии базы: {copy_db} (боевая {live_db} не затрагивается)")

try:
    import app as application_module  # noqa: E402  (runs init_db() on the COPY)
except Exception:
    print("\nОШИБКА при запуске приложения / обновлении схемы базы:")
    traceback.print_exc()
    raise SystemExit(2)

application_module.send_telegram_notification_to_employee = lambda *a, **k: "stubbed"
application_module.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)


def main():
    order_id, employee, day = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    import sqlite3
    with sqlite3.connect(copy_db) as probe:
        row = probe.execute(
            "SELECT id FROM tuning_order_items WHERE order_id = ? AND status != 'removed' ORDER BY id LIMIT 1",
            (order_id,),
        ).fetchone()
        crew = probe.execute("SELECT id FROM employees WHERE name = ?", (employee,)).fetchone()
    if row is None:
        print(f"В заказе №{order_id} нет работ.")
        return
    if crew is None:
        print(f"Сотрудника «{employee}» нет в базе (проверьте написание имени).")
        return
    item_id, employee_id = row[0], crew[0]
    print(f"Заказ №{order_id}, работа №{item_id}, сотрудник «{employee}» (id {employee_id}), дата {day}")
    client = application_module.app.test_client()
    with client.session_transaction() as session:
        session["admin_id"] = 1
        session["admin_name"] = "Администратор"

    def step(title, call):
        print(f"\n== {title}")
        try:
            response = call()
        except Exception:
            print("ОШИБКА:")
            traceback.print_exc()
            return None
        print(f"   HTTP {response.status_code}", response.headers.get("Location", ""))
        return response

    form = {
        "employee_name[]": [employee], "rate[]": ["100"], "norm_hours[]": ["2"],
        "comment": "диагностика", "due_from": day, "due_to": "", "next": "board",
    }
    assign_url = f"/tuning/{order_id}/item/{item_id}/assign"
    step("1. Поручение на дату", lambda: client.post(assign_url, data=form))
    page = step("2. Страница доски (должно появиться окно про смену)",
                lambda: client.get(f"/tuning/{order_id}/board"))
    if page is not None:
        print("   окно про смену на странице:", "Сотрудник не стоит в расписании" in page.get_data(as_text=True))
    step("3. Согласие: добавить в смену и поручить", lambda: client.post(
        assign_url, data=dict(form, shift_decision="add", shift_start="09:00", shift_end="18:00")))
    step("4. Страница доски после поручения", lambda: client.get(f"/tuning/{order_id}/board"))
    step(f"5. Расписание тюнинга на {day}", lambda: client.get(f"/schedule/tuning?date={day}"))
    step(f"6. Добавление в смену со страницы расписания", lambda: client.post(
        "/schedule/tuning/crew", data={"work_date": day, "employee_id": str(employee_id),
                                       "shift_start": "09:00", "shift_end": "18:00"}))
    print("\nГотово. Если ни в одном шаге нет «ОШИБКА» — на копии базы всё работает.")


try:
    main()
finally:
    shutil.rmtree(copy_dir, ignore_errors=True)

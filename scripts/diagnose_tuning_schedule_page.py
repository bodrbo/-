#!/usr/bin/env python3
"""Opens the tuning schedule page on a COPY of the live database and prints
the real traceback of whatever fails (plus the data rows that look odd). The
live database is never written: the script copies it to a temp file first and
points the app at the copy.

Usage:
    python3 scripts/diagnose_tuning_schedule_page.py [YYYY-MM-DD ...]
Without dates: today and the dates that have schedule tasks around it.
"""

import datetime as dt
import os
import shutil
import sqlite3
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
if not os.path.exists(live_db):
    print(f"Базы {live_db} нет.")
    raise SystemExit(1)
copy_dir = tempfile.mkdtemp(prefix="tuning-schedule-diagnose-")
copy_db = os.path.join(copy_dir, "copy.db")
shutil.copy(live_db, copy_db)
os.environ["WORKHOURS_DB_PATH"] = copy_db
print(f"Работаю на копии базы: {copy_db} (боевая {live_db} не затрагивается)")
print(f"SQLite {sqlite3.sqlite_version}, Python {sys.version.split()[0]}")

try:
    import app as application_module  # noqa: E402  (runs init_db() on the COPY)
except Exception:
    print("\nОШИБКА при запуске приложения / обновлении схемы базы:")
    traceback.print_exc()
    raise SystemExit(2)

application_module.app.config.update(TESTING=True, PROPAGATE_EXCEPTIONS=True)


def odd_rows():
    probe = sqlite3.connect(copy_db)
    probe.row_factory = sqlite3.Row
    print("\n== Странные записи расписания")
    ghosts = probe.execute(
        "SELECT t.id, t.assignment_id, t.employee_name, t.title FROM tuning_schedule_tasks t "
        "WHERE t.assignment_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM tuning_item_assignments a WHERE a.id = t.assignment_id)"
    ).fetchall()
    print(f"   карточки задач, чьей задачи заказа уже нет: {len(ghosts)}")
    for row in ghosts[:10]:
        print("     ", dict(row))
    no_days = probe.execute(
        "SELECT COUNT(*) FROM tuning_schedule_tasks t "
        "WHERE NOT EXISTS (SELECT 1 FROM tuning_schedule_task_days d WHERE d.task_id = t.id)"
    ).fetchone()[0]
    print(f"   задачи расписания без единого дня: {no_days}")
    try:
        members = probe.execute("SELECT COUNT(*) FROM tuning_schedule_task_members").fetchone()[0]
        print(f"   участников задач (несколько сотрудников): {members}")
    except sqlite3.Error as error:
        print("   таблица участников:", error)
    columns = [row[1] for row in probe.execute("PRAGMA table_info(tuning_schedule_task_members)")]
    print("   колонки tuning_schedule_task_members:", columns)
    probe.close()


def candidate_days():
    probe = sqlite3.connect(copy_db)
    today = dt.date.today()
    rows = probe.execute(
        "SELECT DISTINCT work_date FROM tuning_schedule_task_days WHERE work_date BETWEEN ? AND ? "
        "ORDER BY work_date", ((today - dt.timedelta(days=3)).isoformat(), (today + dt.timedelta(days=21)).isoformat()),
    ).fetchall()
    probe.close()
    return [today.isoformat()] + [row[0] for row in rows if row[0] != today.isoformat()]


def main():
    days = sys.argv[1:] or candidate_days()
    odd_rows()
    client = application_module.app.test_client()
    with client.session_transaction() as session:
        session["admin_id"] = 1
        session["admin_name"] = "Администратор"
    failed = 0
    for day in days:
        print(f"\n== /schedule/tuning?date={day}")
        try:
            response = client.get(f"/schedule/tuning?date={day}")
            print(f"   HTTP {response.status_code}")
        except Exception:
            failed += 1
            print("   ОШИБКА:")
            traceback.print_exc()
    print("\nГотово.", "Ошибок нет — на копии базы страница открывается." if not failed else f"Страниц с ошибкой: {failed}.")


try:
    main()
finally:
    shutil.rmtree(copy_dir, ignore_errors=True)

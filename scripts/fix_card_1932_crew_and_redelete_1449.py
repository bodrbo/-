#!/usr/bin/env python3
"""One-time follow-up to cleanup_backfill_double_bookings_2026_09.py:
cards #1449 and #1932 are the same real booking (same client, phone,
price, time) — but #1932 (the one kept, linked to accounting trip
№496) has its crew recorded as "Тоже хороший человек!", a YCLIENTS
placeholder/staff-account name, not the real captain (Игорь
Севостьянов) that #1449 correctly had. Fixes #1932's crew assignment
to the real employee, then soft-deletes #1449 again as the now-true
duplicate — same path as the rest of that batch.

Aborts without changing anything if the employee name doesn't match
exactly one active employee, or if #1932's current crew isn't the
expected placeholder (in case someone already touched it by hand).

Usage:
    python3 scripts/fix_card_1932_crew_and_redelete_1449.py
"""

import datetime as dt
import os
import sys
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
        os.environ.setdefault(
            key.strip(), value.strip().strip('"').strip("'")
        )


load_env_file(PROJECT_ROOT.parent / ".env")
load_env_file(PROJECT_ROOT / ".env")

import app as application_module  # noqa: E402
from modules.schedule import services as schedule_services  # noqa: E402

CARD_ID = 1932
DELETE_ID = 1449
PLACEHOLDER_NAME = "Тоже хороший человек!"
ROLE = "guide_captain"


def main():
    with application_module.app.app_context():
        db = application_module.get_db()

        candidates = db.execute(
            "SELECT id, name, deleted_at FROM employees WHERE name LIKE ? "
            "ORDER BY deleted_at IS NULL DESC",
            ("%Севост%",),
        ).fetchall()
        if len(candidates) != 1:
            names = ", ".join(f"{c['id']}:{c['name']!r}" for c in candidates)
            raise SystemExit(
                f"Ожидался ровно один сотрудник с именем на «Севост», "
                f"найдено {len(candidates)}: [{names}]. Ничего не менял — "
                "уточните точное имя и правьте вручную."
            )
        employee = candidates[0]
        print(f"Реальный капитан: id={employee['id']} name={employee['name']!r} "
              f"(deleted_at={employee['deleted_at']})")

        card = db.execute(
            "SELECT id, starts_at, deleted_at FROM schedule_items WHERE id = ?",
            (CARD_ID,),
        ).fetchone()
        if card is None or card["deleted_at"] is not None:
            raise SystemExit(f"Карточка №{CARD_ID} не найдена или уже удалена.")

        current_assignment = db.execute(
            "SELECT id, employee_id, employee_name, role FROM schedule_assignments "
            "WHERE schedule_item_id = ?",
            (CARD_ID,),
        ).fetchall()
        if len(current_assignment) != 1 or current_assignment[0]["employee_name"] != PLACEHOLDER_NAME:
            found = [dict(row) for row in current_assignment]
            raise SystemExit(
                f"Экипаж карточки №{CARD_ID} не совпадает с ожидаемым "
                f"(один сотрудник «{PLACEHOLDER_NAME}»). Сейчас: {found}. "
                "Ничего не менял — проверьте вручную."
            )

        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        db.execute(
            "DELETE FROM schedule_assignments WHERE schedule_item_id = ?",
            (CARD_ID,),
        )
        db.execute(
            "INSERT INTO schedule_assignments "
            "(schedule_item_id, employee_id, employee_name, role, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (CARD_ID, employee["id"], employee["name"], ROLE, timestamp),
        )
        db.execute(
            "INSERT OR IGNORE INTO schedule_day_crew "
            "(work_date, employee_id, created_at) VALUES (?, ?, ?)",
            (card["starts_at"][:10], employee["id"], timestamp),
        )
        db.execute(
            "UPDATE schedule_items SET updated_at = ? WHERE id = ?",
            (timestamp, CARD_ID),
        )
        db.commit()
        print(f"Экипаж карточки №{CARD_ID} исправлен на {employee['name']!r} ({ROLE}).")

        ok, message = schedule_services.delete_item(db, DELETE_ID)
        print(f"Повторное удаление №{DELETE_ID}: {ok} — {message}")


if __name__ == "__main__":
    main()

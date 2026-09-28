#!/usr/bin/env python3
"""Finds tuning tasks that are "Выполнена" but have no payroll entry (never
created, or deleted on the Зарплаты page) and, with --apply, creates the
missing payouts (rate x norm-hours, dated with the task's completion date,
else today). Without --apply it only lists them — nothing is written.

Usage:
    python3 scripts/repair_tuning_task_payouts.py           # list only
    python3 scripts/repair_tuning_task_payouts.py --apply   # create the payouts
"""

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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(PROJECT_ROOT.parent / ".env")
load_env_file(PROJECT_ROOT / ".env")

import app as application_module  # noqa: E402


def main():
    apply_changes = "--apply" in sys.argv[1:]
    with application_module.app.app_context():
        db = application_module.get_db()
        missing = []
        for row in db.execute(
            "SELECT tia.*, ti.order_id, ti.work_name FROM tuning_item_assignments tia "
            "JOIN tuning_order_items ti ON ti.id = tia.item_id "
            "WHERE tia.assignment_status = 'done' ORDER BY ti.order_id, tia.id"
        ).fetchall():
            if not application_module._tuning_assignment_has_payout(db, row):
                missing.append(row)
        print(f"Выполненных задач без выплаты: {len(missing)}")
        for row in missing:
            amount = row["rate"] * row["norm_hours"]
            day = (row["completed_at"] or "")[:10] or "сегодня"
            print(f"  заказ №{row['order_id']} «{row['work_name']}» — {row['employee_name']}: "
                  f"{amount:,.2f} ₽, дата выплаты {day}")
        if not missing:
            return 0
        if not apply_changes:
            print("\nЗапись в базу не выполнялась. Добавьте --apply, чтобы создать выплаты.")
            return 0
        created = application_module._repair_tuning_assignment_payouts(db)
        db.commit()
        print(f"\nСоздано выплат: {len(created)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

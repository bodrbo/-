#!/usr/bin/env python3
"""Read-only: the whole accounting story of ONE schedule card — is it a
candidate for auto-close, was it already closed into a trip, and which payroll
entries exist for it. Makes no writes.

Usage:
    python3 scripts/diagnose_schedule_item_closing.py <schedule_item_id>
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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(PROJECT_ROOT.parent / ".env")
load_env_file(PROJECT_ROOT / ".env")

import app as application_module  # noqa: E402
from modules.payroll_rates import repository as payroll_rates_repository  # noqa: E402
from modules.schedule import repository as schedule_repository  # noqa: E402
from modules.schedule import services as schedule_services  # noqa: E402


def show(row, keys):
    for key in keys:
        print(f"   {key}: {row[key] if key in row.keys() else '—'}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    item_id = int(sys.argv[1])
    now = dt.datetime.now()
    with application_module.app.app_context():
        db = application_module.get_db()
        item = db.execute("SELECT * FROM schedule_items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            print(f"Карточки №{item_id} нет в базе.")
            return 1
        print(f"== Карточка №{item_id}")
        show(item, [
            "kind", "service_name", "boat", "starts_at", "ends_at", "revenue", "source",
            "status", "deleted_at", "accounting_trip_id", "payroll_closed_at",
            "merged_into_item_id", "tripster_resolved",
        ])
        assignments = schedule_repository.list_assignments(db, item_id)
        print("== Экипаж:", ", ".join(f"{a['employee_name']} ({a['role']})" for a in assignments) or "— нет —")

        cutoff = now - dt.timedelta(minutes=schedule_services.AUTO_CLOSE_GRACE_MINUTES)
        since = now - dt.timedelta(days=schedule_services.AUTO_CLOSE_LOOKBACK_DAYS)
        print("== Проверка условий автозакрытия:")
        checks = [
            ("не удалена (deleted_at пуст)", item["deleted_at"] is None),
            ("ещё не связана с рейсом (accounting_trip_id пуст)", item["accounting_trip_id"] is None),
            ("зарплата ещё не начислена (payroll_closed_at пуст)", not item["payroll_closed_at"]),
            (f"закончилась ≥{schedule_services.AUTO_CLOSE_GRACE_MINUTES} мин назад ({item['ends_at']})",
             item["ends_at"] <= cutoff.strftime("%Y-%m-%d %H:%M")),
            (f"не старше {schedule_services.AUTO_CLOSE_LOOKBACK_DAYS} дней",
             item["ends_at"] >= since.strftime("%Y-%m-%d %H:%M")),
            ("есть экипаж", bool(assignments)),
        ]
        for label, ok in checks:
            print(f"   [{'да' if ok else 'НЕТ'}] {label}")

        if item["boat"] and assignments and item["accounting_trip_id"] is None:
            starts = dt.datetime.strptime(item["starts_at"], "%Y-%m-%d %H:%M")
            ends = dt.datetime.strptime(item["ends_at"], "%Y-%m-%d %H:%M")
            hours = round(max((ends - starts).total_seconds() / 3600, 0), 2)
            payload = {
                "boat": item["boat"], "trip_date": item["starts_at"][:10],
                "trip_time": item["starts_at"][11:16], "revenue": item["revenue"],
                "sale_channel": "direct", "commission_pct": 0, "fuel_cost": 0, "mooring_cost": 0,
                "labor_items": [
                    {"employee": a["employee_name"], "work_type": item["service_name"],
                     "quantity": hours,
                     "rate": payroll_rates_repository.get_excursion_role_rate(db, a["role"]) or 0}
                    for a in assignments
                ],
            }
            errors, _ = application_module._process_trip_form(
                db, application_module._payload_to_form(payload)
            )
            print("== Проверка формы рейса:", "; ".join(errors) if errors else "ошибок нет")

        if item["accounting_trip_id"] is not None:
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (item["accounting_trip_id"],)).fetchone()
            print(f"== Связанный рейс trips №{item['accounting_trip_id']}:")
            if trip is None:
                print("   ЗАПИСИ НЕТ (ссылка на удалённый рейс)")
            else:
                show(trip, ["boat", "trip_date", "trip_time", "work_type", "revenue", "labor_cost",
                            "my_share", "investor_payout", "source", "needs_review"])
                links = db.execute(
                    "SELECT e.* FROM trip_labor tl JOIN entries e ON e.id = tl.entry_id WHERE tl.trip_id = ?",
                    (trip["id"],),
                ).fetchall()
                print(f"== Записи зарплаты по рейсу: {len(links)}")
                for e in links:
                    print(f"   #{e['id']} {e['employee']} | {e['work_type']} | {e['work_date']} | "
                          f"{e['quantity']} ч × {e['rate']} = {e['amount']}")
        entries = db.execute("SELECT * FROM entries WHERE schedule_item_id = ?", (item_id,)).fetchall()
        if entries:
            print(f"== Записи зарплаты по карточке без рейса (городская): {len(entries)}")
            for e in entries:
                print(f"   #{e['id']} {e['employee']} | {e['work_date']} | {e['amount']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

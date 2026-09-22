#!/usr/bin/env python3
"""Read-only diagnostic for a single schedule card that fails to save with
an "already occupied" error (boat or crew conflict) — reproduces the exact
checks modules.schedule.services.validate_item_form runs on save
(repository.find_boat_conflicts / find_employee_conflicts, both correctly
excluding the card's own id) so the actual colliding card, if any, can be
identified without guessing. Makes no writes.

Usage:
    python3 scripts/diagnose_schedule_card_conflict.py <schedule_item_id>
"""

import argparse
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("card_id", type=int, help="schedule_items.id")
    args = parser.parse_args()
    card_id = args.card_id

    with application_module.app.app_context():
        db = application_module.get_db()

        card = db.execute(
            "SELECT * FROM schedule_items WHERE id = ?", (card_id,)
        ).fetchone()
        if card is None:
            raise SystemExit(f"Карточка №{card_id} не найдена.")

        print(f"Карточка №{card_id}:")
        for key in card.keys():
            print(f"  {key} = {card[key]}")

        assignments = db.execute(
            "SELECT * FROM schedule_assignments WHERE schedule_item_id = ?",
            (card_id,),
        ).fetchall()
        print("\nЭкипаж карточки:")
        for a in assignments:
            print(f"  employee_id={a['employee_id']} {a['employee_name']} role={a['role']}")

        print("\n--- Проверка конфликта по катеру (та же логика, что при сохранении) ---")
        boat_conflicts = db.execute(
            "SELECT id, kind, service_name, starts_at, ends_at, deleted_at "
            "FROM schedule_items "
            "WHERE deleted_at IS NULL AND boat = ? "
            "AND starts_at < ? AND ends_at > ? AND id != ?",
            (card["boat"], card["ends_at"], card["starts_at"], card_id),
        ).fetchall()
        if boat_conflicts:
            print(
                f"Найдены {len(boat_conflicts)} других карточек на катере "
                f"«{card['boat']}», пересекающихся по времени с №{card_id}:"
            )
            for c in boat_conflicts:
                print(
                    f"  №{c['id']} [{c['kind']}] {c['service_name']} "
                    f"{c['starts_at']}–{c['ends_at']} (deleted_at={c['deleted_at']})"
                )
        else:
            print("Других карточек на этом катере, пересекающихся по времени, НЕ найдено.")

        print("\n--- Проверка конфликта по сотрудникам ---")
        employee_ids = [a["employee_id"] for a in assignments]
        if employee_ids:
            placeholders = ",".join("?" for _ in employee_ids)
            emp_conflicts = db.execute(
                "SELECT DISTINCT schedule_items.id, schedule_items.service_name, "
                "schedule_items.starts_at, schedule_items.ends_at, "
                "schedule_assignments.employee_name FROM schedule_items "
                "JOIN schedule_assignments "
                "ON schedule_assignments.schedule_item_id = schedule_items.id "
                "WHERE schedule_items.deleted_at IS NULL "
                f"AND schedule_assignments.employee_id IN ({placeholders}) "
                "AND schedule_items.starts_at < ? AND schedule_items.ends_at > ? "
                "AND schedule_items.id != ?",
                (*employee_ids, card["ends_at"], card["starts_at"], card_id),
            ).fetchall()
            if emp_conflicts:
                print(
                    f"Найдены {len(emp_conflicts)} других карточек, где заняты "
                    f"те же сотрудники в это время:"
                )
                for c in emp_conflicts:
                    print(
                        f"  №{c['id']} {c['employee_name']} — {c['service_name']} "
                        f"{c['starts_at']}–{c['ends_at']}"
                    )
            else:
                print("Конфликтов по сотрудникам не найдено.")
        else:
            print("У карточки нет назначенного экипажа.")

        if card["accounting_trip_id"]:
            trip = db.execute(
                "SELECT * FROM trips WHERE id = ?", (card["accounting_trip_id"],)
            ).fetchone()
            print(f"\nСвязанный рейс в учёте (trips.id={card['accounting_trip_id']}):")
            if trip:
                print(
                    f"  boat={trip['boat']} trip_date={trip['trip_date']} "
                    f"trip_time={trip['trip_time']} source={trip['source']}"
                )
            else:
                print(
                    "  НЕ НАЙДЕН (accounting_trip_id указывает в никуда — "
                    "возможная причина ошибки)."
                )


if __name__ == "__main__":
    main()

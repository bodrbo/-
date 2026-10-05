#!/usr/bin/env python3
"""Read-only: finds schedule cards whose linked accounting trip
(schedule_items.accounting_trip_id) no longer matches the card — different
boat, different crew, or different revenue. Such a card counts as "already
closed", so auto-close skips it and the crew actually on the card is never
paid (e.g. card 1936: linked to YCLIENTS trip 498 on another boat/captain).
Makes no writes.

Usage:
    python3 scripts/find_card_trip_mismatches.py [YYYY-MM-DD]   # cards from this date (default 2026-09-01)
    python3 scripts/find_card_trip_mismatches.py --card 1936     # one card in detail
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


def crew_of_card(db, item_id):
    return sorted(r["employee_name"] for r in db.execute(
        "SELECT employee_name FROM schedule_assignments WHERE schedule_item_id = ?", (item_id,)
    ).fetchall())


def crew_of_trip(db, trip_id):
    return sorted({r["employee"] for r in db.execute(
        "SELECT e.employee FROM trip_labor tl JOIN entries e ON e.id = tl.entry_id "
        "WHERE tl.trip_id = ?", (trip_id,)
    ).fetchall()})


def mismatches(db, item, trip):
    problems = []
    if (item["boat"] or "") != (trip["boat"] or ""):
        problems.append(f"катер: карточка «{item['boat']}» ≠ рейс «{trip['boat']}»")
    card_crew, trip_crew = crew_of_card(db, item["id"]), crew_of_trip(db, trip["id"])
    if card_crew != trip_crew:
        problems.append(f"экипаж: карточка {card_crew} ≠ зарплата рейса {trip_crew}")
    if round(item["revenue"] or 0) != round(trip["revenue"] or 0):
        problems.append(f"выручка: карточка {item['revenue']} ≠ рейс {trip['revenue']}")
    return problems


def main():
    args = sys.argv[1:]
    with application_module.app.app_context():
        db = application_module.get_db()
        if args and args[0] == "--card":
            item = db.execute("SELECT * FROM schedule_items WHERE id = ?", (int(args[1]),)).fetchone()
            if item is None:
                print("Карточки нет.")
                return 1
            print("== История карточки")
            for key in ("created_at", "updated_at", "source", "note", "boat", "revenue", "starts_at"):
                print(f"   {key}: {item[key] if key in item.keys() else '—'}")
            cols = [r["name"] for r in db.execute("PRAGMA table_info(schedule_assignments)").fetchall()]
            for a in db.execute(
                "SELECT * FROM schedule_assignments WHERE schedule_item_id = ?", (item["id"],)
            ).fetchall():
                print("   экипаж:", {c: a[c] for c in cols})
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (item["accounting_trip_id"],)).fetchone()
            if trip is not None:
                print("== Расхождения с рейсом:", "; ".join(mismatches(db, item, trip)) or "нет")
                imp = db.execute(
                    "SELECT * FROM yclients_imports WHERE trip_id = ?", (trip["id"],)
                ).fetchall()
                print(f"== Записи YCLIENTS, из которых создан рейс №{trip['id']}: {len(imp)}")
                for row in imp:
                    print("  ", dict(row))
            others = db.execute(
                "SELECT id, boat, starts_at, accounting_trip_id, deleted_at FROM schedule_items "
                "WHERE substr(starts_at, 1, 10) = ? AND id != ? ORDER BY starts_at",
                (item["starts_at"][:10], item["id"]),
            ).fetchall()
            print(f"== Другие карточки этого дня: {len(others)}")
            for o in others:
                print(f"   №{o['id']} {o['starts_at']} {o['boat']} trip={o['accounting_trip_id']} "
                      f"{'(удалена)' if o['deleted_at'] else ''}")
            return 0

        since = args[0] if args else "2026-09-01"
        rows = db.execute(
            "SELECT * FROM schedule_items WHERE deleted_at IS NULL AND accounting_trip_id IS NOT NULL "
            "AND substr(starts_at, 1, 10) >= ? ORDER BY starts_at", (since,)
        ).fetchall()
        found = 0
        for item in rows:
            trip = db.execute("SELECT * FROM trips WHERE id = ?", (item["accounting_trip_id"],)).fetchone()
            if trip is None:
                print(f"№{item['id']} {item['starts_at']}: ссылка на несуществующий рейс {item['accounting_trip_id']}")
                found += 1
                continue
            problems = mismatches(db, item, trip)
            if problems:
                found += 1
                print(f"№{item['id']} {item['starts_at']} «{item['boat']}» → рейс №{trip['id']} "
                      f"({trip['source']}): " + "; ".join(problems))
        print(f"Проверено карточек: {len(rows)}, с расхождениями: {found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

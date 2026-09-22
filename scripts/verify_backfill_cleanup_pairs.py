#!/usr/bin/env python3
"""Read-only re-check of the 7 pairs cleaned up in
scripts/cleanup_backfill_double_bookings_2026_09.py — #1449 turned out
to be a real, separate booking (Игорь Севостянов) wrongly deleted, not
a duplicate of #1932, so every other pair needs the same scrutiny
before trusting the cleanup. Shows both sides of each pair in full,
including the "deleted" side (soft delete only sets deleted_at, so the
data is still there to compare), plus the kept side's linked trip and
(for event-kind cards) its participants — so a human can tell whether
each pair was really the same physical trip or two different real
bookings that happened to share a boat/time by coincidence. Makes no
writes.

Usage:
    python3 scripts/verify_backfill_cleanup_pairs.py
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
        os.environ.setdefault(
            key.strip(), value.strip().strip('"').strip("'")
        )


load_env_file(PROJECT_ROOT.parent / ".env")
load_env_file(PROJECT_ROOT / ".env")

import app as application_module  # noqa: E402

# (deleted-side id, kept-side id, label) — same pairs as the cleanup script.
PAIRS = [
    (1433, 1918, "Бодрый Второй, 2026-09-14"),
    (1438, 1931, "Бодрый Второй, 2026-09-22"),
    (1944, 1945, "Бодрый Второй, 2026-09-23"),
    (1432, 1911, "Бодрый Первый, 2026-09-12"),
    (1436, 1917, "Бодрый Первый, 2026-09-14"),
    (1015, 1919, "Бодрый Первый, 2026-09-14"),
    (1449, 1932, "Бодрый Первый, 2026-09-22 (уже восстановлена)"),
]


def print_card(db, item_id, tag):
    row = db.execute("SELECT * FROM schedule_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        print(f"  [{tag}] №{item_id}: НЕ НАЙДЕНА В БАЗЕ")
        return
    print(
        f"  [{tag}] №{item_id} [{row['kind']}, source={row['source']}] "
        f"{row['service_name']} {row['starts_at']}-{row['ends_at']} "
        f"deleted_at={row['deleted_at']} revenue={row['revenue']}"
    )
    print(f"      клиент (флаг. поля): {row['customer_name']!r} / {row['customer_phone']!r}")
    participants = db.execute(
        "SELECT client_name, client_phone, guests_count, price FROM schedule_participants "
        "WHERE schedule_item_id = ?",
        (item_id,),
    ).fetchall()
    for p in participants:
        print(
            f"      участник: {p['client_name']} / {p['client_phone']} "
            f"гостей={p['guests_count']} цена={p['price']}"
        )
    assignments = db.execute(
        "SELECT employee_name, role FROM schedule_assignments WHERE schedule_item_id = ?",
        (item_id,),
    ).fetchall()
    for a in assignments:
        print(f"      экипаж: {a['employee_name']} ({a['role']})")
    if row["accounting_trip_id"]:
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?", (row["accounting_trip_id"],)
        ).fetchone()
        if trip:
            print(
                f"      рейс в учёте №{trip['id']}: {trip['boat']} "
                f"{trip['trip_date']} {trip['trip_time']} revenue={trip['revenue']} "
                f"source={trip['source']}"
            )


def main():
    with application_module.app.app_context():
        db = application_module.get_db()
        for deleted_id, kept_id, label in PAIRS:
            print(f"\n=== {label} ===")
            print_card(db, deleted_id, "удалена")
            print_card(db, kept_id, "оставлена")


if __name__ == "__main__":
    main()

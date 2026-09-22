#!/usr/bin/env python3
"""One-time cleanup for the specific duplicate schedule cards the
2026-09-22 backfill bug created (fixed in "Stop the YCLIENTS backfill
from double-booking an already-occupied slot") — found via
scripts/find_duplicate_schedule_cards.py and reviewed with the business
owner. Each pair below is the same real trip entered twice: one card
with no accounting_trip_id (an orphaned Tripster/manual entry the
backfill didn't know about) and one with accounting_trip_id set (the
financially-linked card the backfill produced from the actual YCLIENTS
trip) — this script soft-deletes the orphaned side of each pair via
modules.schedule.services.delete_item, the exact same call the "Удалить"
button in the schedule UI makes. Idempotent: re-running is safe, an
already-deleted or already-linked card is reported and skipped.

Deliberately NOT a general-purpose tool — the id list is specific to
this one cleanup, reviewed by hand. Does not touch the much larger set
of "Не назначен" Tripster booking/event pairs found by the same scan;
those look like separate traveler records for a shared group slot, not
duplicates of one booking, and need a separate look at Tripster's own
grouping logic before anything there is touched.

Usage:
    python3 scripts/cleanup_backfill_double_bookings_2026_09.py
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
from modules.schedule import services as schedule_services  # noqa: E402

# (id to delete, id being kept) — kept side always has accounting_trip_id set.
PAIRS = [
    (1433, 1918, "Бодрый Второй, 2026-09-14"),
    (1438, 1931, "Бодрый Второй, 2026-09-22"),
    (1944, 1945, "Бодрый Второй, 2026-09-23"),
    (1432, 1911, "Бодрый Первый, 2026-09-12"),
    (1436, 1917, "Бодрый Первый, 2026-09-14"),
    (1015, 1919, "Бодрый Первый, 2026-09-14"),
    (1449, 1932, "Бодрый Первый, 2026-09-22"),
]


def main():
    with application_module.app.app_context():
        db = application_module.get_db()
        for delete_id, keep_id, label in PAIRS:
            kept = db.execute(
                "SELECT id, deleted_at, accounting_trip_id FROM schedule_items WHERE id = ?",
                (keep_id,),
            ).fetchone()
            if kept is None or kept["deleted_at"] is not None or not kept["accounting_trip_id"]:
                print(
                    f"[{label}] пропущено: карточка №{keep_id}, которую нужно "
                    "оставить, не в ожидаемом состоянии — проверьте вручную."
                )
                continue
            ok, message = schedule_services.delete_item(db, delete_id)
            print(f"[{label}] удаление №{delete_id} (оставляем №{keep_id}): {ok} — {message}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Read-only scan for schedule cards double-booking the same boat/time —
the production shape of the bug fixed in create_item_from_trip (see
"Stop the YCLIENTS backfill from double-booking an already-occupied
slot"): two live schedule_items rows on the same boat with overlapping
starts_at/ends_at, almost always because one came from the YCLIENTS
backfill and the other from Tripster's own sync (or a manual entry) for
the same real trip. Makes no writes — only lists what it finds so a
human decides which card of each pair to keep.

Usage:
    python3 scripts/find_duplicate_schedule_cards.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]

Default range is the whole schedule (no date filter).
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
    parser.add_argument("--start", default=None, help="YYYY-MM-DD, optional")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD, optional")
    args = parser.parse_args()

    with application_module.app.app_context():
        db = application_module.get_db()
        query = (
            "SELECT id, boat, kind, service_name, starts_at, ends_at, "
            "source, accounting_trip_id FROM schedule_items "
            "WHERE deleted_at IS NULL"
        )
        params = []
        if args.start:
            query += " AND starts_at >= ?"
            params.append(f"{args.start} 00:00")
        if args.end:
            query += " AND starts_at <= ?"
            params.append(f"{args.end} 23:59")
        query += " ORDER BY boat, starts_at"
        items = db.execute(query, params).fetchall()

    pairs_found = 0
    reported_ids = set()
    for i, item in enumerate(items):
        for other in items[i + 1:]:
            if other["boat"] != item["boat"]:
                break
            if other["starts_at"] >= item["ends_at"]:
                break
            if item["id"] in reported_ids and other["id"] in reported_ids:
                continue
            pairs_found += 1
            reported_ids.add(item["id"])
            reported_ids.add(other["id"])
            print(f"\nКатер «{item['boat']}»:")
            for row in (item, other):
                print(
                    f"  №{row['id']} [{row['kind']}, source={row['source']}] "
                    f"{row['service_name']} {row['starts_at']}–{row['ends_at']} "
                    f"accounting_trip_id={row['accounting_trip_id']}"
                )

    if not pairs_found:
        print("Пересекающихся карточек на одном катере не найдено.")
    else:
        print(f"\nВсего найдено пар: {pairs_found}.")


if __name__ == "__main__":
    main()

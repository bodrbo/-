#!/usr/bin/env python3
"""One-time historical backfill of YCLIENTS trips into the local `trips`
table, run by hand before the internal schedule takes over as the source
of payroll/investor data (see the "Переход с YClients на внутреннее
расписание" plan). Not a cron job and not an HTTP route on purpose — a
multi-year date range risks a gateway timeout through /trips/import/fetch
on Beget hosting, and this is meant to run exactly once, not be re-triggered
casually from the UI.

Reuses the same _import_yclients_trip_records pipeline the manual "Добавить
рейс"-adjacent import page and the (soon to be retired) hourly cron already
share, batched month by month so a single YCLIENTS API call never risks its
own 100-page safety valve. Idempotent (keyed on yclients_imports.yclients_ref)
— an overly wide start date only costs extra API calls, never duplicate
trips, so it's safe to re-run if interrupted.

Also idempotent against the internal schedule module's own auto-close job
(see modules/schedule/services.py::auto_close_schedule_items, live since
2026-09-22): _try_auto_import_candidate now checks for an existing manual
or schedule-auto trip on the same boat/date/time before auto-confirming a
YCLIENTS candidate, and if one is found, queues the candidate for manual
review instead of creating a duplicate. This matters because the same
real trip can get entered twice during the migration — once by hand/via
the schedule, once via YCLIENTS — and both pipelines are live at once
right now (the hourly YCLIENTS cron hasn't been cut over yet).

Usage:
    python3 scripts/backfill_yclients_trips.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]

Default range is 2026-04-01 through today — trips before that date are
out of scope for this backfill by business decision. After this finishes,
check /trips "Ожидают подтверждения" for anything that didn't import
cleanly (unmapped work type, unresolved boat color, a likely duplicate
against an existing trip, etc.) — those need a manual look, same as any
other YCLIENTS import candidate.
"""

import argparse
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


def month_chunks(start_date, end_date):
    """Yield (chunk_start, chunk_end) date pairs covering [start_date, end_date],
    one calendar month at a time."""
    cursor = start_date.replace(day=1)
    while cursor <= end_date:
        if cursor.month == 12:
            next_month = cursor.replace(year=cursor.year + 1, month=1)
        else:
            next_month = cursor.replace(month=cursor.month + 1)
        chunk_start = max(cursor, start_date)
        chunk_end = min(next_month - dt.timedelta(days=1), end_date)
        yield chunk_start, chunk_end
        cursor = next_month


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-04-01", help="YYYY-MM-DD, default 2026-04-01")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD, default today")
    args = parser.parse_args()

    if not application_module.yclients_configured():
        raise SystemExit(
            "YCLIENTS_PARTNER_TOKEN, YCLIENTS_USER_TOKEN и "
            "YCLIENTS_COMPANY_ID должны быть заданы в .env."
        )

    start_date = dt.date.fromisoformat(args.start)
    end_date = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    if start_date > end_date:
        raise SystemExit("--start должен быть не позже --end.")

    totals = {
        "fetched": 0, "cancelled": 0, "deleted": 0,
        "added": 0, "merged": 0, "imported": 0, "payroll_updated": 0,
        "topups_changed": 0,
    }
    last_pending = 0

    with application_module.app.app_context():
        db = application_module.get_db()
        now = dt.datetime.now()
        for chunk_start, chunk_end in month_chunks(start_date, end_date):
            chunk_start_iso = chunk_start.isoformat()
            chunk_end_iso = chunk_end.isoformat()
            print(f"[{chunk_start_iso} .. {chunk_end_iso}] запрашиваю YCLIENTS…", flush=True)
            records = application_module.yclients_get_records(chunk_start_iso, chunk_end_iso)
            activity_ids = {
                r["activity_id"] for r in records if r.get("activity_id")
            }
            activity_colors = application_module.yclients_get_activity_colors(activity_ids)
            completed_records = application_module._yclients_completed_records(records, now)

            stats = application_module._import_yclients_trip_records(
                db,
                completed_records,
                activity_colors,
                chunk_start_iso,
                chunk_end_iso,
                now=now,
                prune_stale=False,
            )
            for key in totals:
                totals[key] += stats.get(key, 0)
            # "pending" is a live snapshot of the whole import_candidates
            # table each call, not a per-chunk delta — keep only the latest.
            last_pending = stats.get("pending", 0)
            print(
                f"  получено {stats['fetched']}, импортировано {stats['imported']}, "
                f"пропущено удалённых/отменённых {stats['cancelled'] + stats['deleted']}, "
                f"сейчас в очереди «Ожидают подтверждения» — {last_pending}",
                flush=True,
            )

    with application_module.app.app_context():
        db = application_module.get_db()
        conflict_count = db.execute(
            "SELECT COUNT(*) AS c FROM import_candidates "
            "WHERE summary LIKE '%Похоже, рейс уже есть в системе%'"
        ).fetchone()["c"]

    print(
        "\nБэкфилл завершён: "
        f"получено {totals['fetched']}, импортировано рейсов {totals['imported']}, "
        f"объединено {totals['merged']}, добавлено новых {totals['added']}, "
        f"отменено {totals['cancelled']}, удалено {totals['deleted']}, "
        f"начислений зарплаты обновлено {totals['payroll_updated']}, "
        f"доплат до минимума изменено {totals['topups_changed']}."
    )
    if conflict_count:
        print(
            f"\n⚠ Из них {conflict_count} — вероятные дубликаты "
            "(рейс на тот же катер/дату/время уже есть в trips, добавлен "
            "вручную или через автозакрытие расписания). Они НЕ импортированы "
            "автоматически — найдите их в «Ожидают подтверждения» на /trips "
            "(текст «Похоже, рейс уже есть в системе») и сверьте вручную."
        )
    if last_pending:
        print(
            f"\nВ очереди «Ожидают подтверждения» на странице /trips сейчас "
            f"{last_pending} записей (включая дубликаты выше, если есть) — "
            "разберите их вручную перед тем как считать бэкфилл завершённым."
        )


if __name__ == "__main__":
    main()

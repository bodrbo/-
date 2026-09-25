#!/usr/bin/env python3
"""Read-only: which finished schedule cards were NOT turned into payroll, and
why. Mirrors modules.schedule.services.auto_close_schedule_items — a card is
skipped (and retried on every cron run, silently) when it has no crew, or when
the trip form validation fails (no boat in Флот, bad number...). Makes no writes.

Usage:
    python3 scripts/diagnose_unclosed_schedule_items.py [days_back=30]
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


def main():
    days_back = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    now = dt.datetime.now()
    since = (now - dt.timedelta(days=days_back)).strftime("%Y-%m-%d 00:00")
    cutoff = (now - dt.timedelta(minutes=schedule_services.AUTO_CLOSE_GRACE_MINUTES)).strftime("%Y-%m-%d %H:%M")
    reasons = {}
    with application_module.app.app_context():
        db = application_module.get_db()
        rows = [
            item for item in schedule_repository.list_items_ready_to_close(db, cutoff)
            if item["starts_at"] >= since
        ]
        print(f"Незакрытых завершённых карточек за {days_back} дн.: {len(rows)}\n")
        for item in rows:
            assignments = schedule_repository.list_assignments(db, item["id"])
            if not assignments:
                reason = "нет назначенного экипажа"
            else:
                starts = dt.datetime.strptime(item["starts_at"], "%Y-%m-%d %H:%M")
                ends = dt.datetime.strptime(item["ends_at"], "%Y-%m-%d %H:%M")
                hours = round(max((ends - starts).total_seconds() / 3600, 0), 2)
                labor = [
                    {
                        "employee": a["employee_name"], "work_type": item["service_name"],
                        "quantity": hours,
                        "rate": payroll_rates_repository.get_excursion_role_rate(db, a["role"]) or 0,
                    }
                    for a in assignments
                ]
                payload = {
                    "boat": item["boat"], "trip_date": item["starts_at"][:10],
                    "trip_time": item["starts_at"][11:16], "revenue": item["revenue"],
                    "sale_channel": "direct", "commission_pct": 0, "fuel_cost": 0,
                    "mooring_cost": 0, "labor_items": labor,
                }
                if not item["boat"]:
                    reason = "без катера (городская экскурсия) — закрывается в зарплату без рейса"
                else:
                    errors, _data = application_module._process_trip_form(
                        db, application_module._payload_to_form(payload)
                    )
                    reason = "; ".join(errors) if errors else "проходит проверку (закроется на ближайшем запуске cron)"
            reasons.setdefault(reason, []).append(item)
        for reason, items in sorted(reasons.items(), key=lambda pair: -len(pair[1])):
            print(f"== {reason}: {len(items)}")
            for item in items[:15]:
                print(f"   №{item['id']} {item['starts_at']} «{item['service_name']}» катер={item['boat'] or '—'}")
            if len(items) > 15:
                print(f"   … и ещё {len(items) - 15}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

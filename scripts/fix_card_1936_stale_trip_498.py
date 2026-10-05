#!/usr/bin/env python3
"""One-time fix: card #1936 (Ларус, 2026-10-03 13:00, captain Платон Жмаев)
is still linked to YCLIENTS trip №498 (Бодрый Второй, Даниил Галецкий,
4400 ₽) — the card was restored from that trip, then edited by hand to the
real data, so auto-close treats it as already closed and the real captain
was never paid. Confirmed with the owner: Даниил did not work this trip.

Deletes the stale trip №498 together with its payroll entry (same cascade as
the "Удалить" button on /trips; the YCLIENTS reference is kept as a
tombstone so the hourly import can't recreate it) and unlinks the card, so
the next auto-close run creates the correct trip for Ларус / Платон Жмаев.

Dry run by default — nothing is changed without --apply. Aborts if the data
isn't exactly what was diagnosed (someone may have already touched it).
A payroll week already marked paid does NOT stop it: the owner confirmed the
real payout was settled by hand and is correct, only the amounts in the
program are wrong, so the week's "paid" mark is left as it is.

Usage:
    python3 scripts/fix_card_1936_stale_trip_498.py            # dry run
    python3 scripts/fix_card_1936_stale_trip_498.py --apply
    python3 scripts/fix_card_1936_stale_trip_498.py --apply --close-now
(--close-now runs the same auto-close pass cron does right after the fix —
for every card ready to close, not only #1936 — instead of waiting for cron.)
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
from modules.schedule import services as schedule_services  # noqa: E402

CARD_ID = 1936
TRIP_ID = 498
CARD_BOAT = "Ларус"
CARD_CAPTAIN = "Платон Жмаев"
STALE_EMPLOYEE = "Даниил Галецкий"


def abort(message):
    print(f"ОСТАНОВЛЕНО, ничего не изменено: {message}")
    return 1


def main():
    apply = "--apply" in sys.argv[1:]
    close_now = "--close-now" in sys.argv[1:]
    with application_module.app.app_context():
        db = application_module.get_db()

        card = db.execute("SELECT * FROM schedule_items WHERE id = ?", (CARD_ID,)).fetchone()
        if card is None or card["deleted_at"] is not None:
            return abort(f"карточки №{CARD_ID} нет или она удалена.")
        if card["accounting_trip_id"] != TRIP_ID:
            return abort(
                f"карточка №{CARD_ID} уже не привязана к рейсу №{TRIP_ID} "
                f"(сейчас: {card['accounting_trip_id']}) — возможно, её уже исправили."
            )
        if card["boat"] != CARD_BOAT:
            return abort(f"у карточки катер «{card['boat']}», ожидался «{CARD_BOAT}».")
        crew = [r["employee_name"] for r in db.execute(
            "SELECT employee_name FROM schedule_assignments WHERE schedule_item_id = ?", (CARD_ID,)
        ).fetchall()]
        if crew != [CARD_CAPTAIN]:
            return abort(f"экипаж карточки {crew}, ожидался [{CARD_CAPTAIN!r}].")

        trip = db.execute("SELECT * FROM trips WHERE id = ?", (TRIP_ID,)).fetchone()
        if trip is None:
            return abort(f"рейса №{TRIP_ID} нет.")
        if trip["source"] != "yclients" or trip["boat"] == CARD_BOAT:
            return abort(
                f"рейс №{TRIP_ID} не похож на ошибочный импорт "
                f"(source={trip['source']}, катер «{trip['boat']}»)."
            )
        entries = db.execute(
            "SELECT e.* FROM trip_labor tl JOIN entries e ON e.id = tl.entry_id WHERE tl.trip_id = ?",
            (TRIP_ID,),
        ).fetchall()
        if [e["employee"] for e in entries] != [STALE_EMPLOYEE]:
            return abort(
                f"зарплата по рейсу №{TRIP_ID}: {[e['employee'] for e in entries]}, "
                f"ожидалась только {STALE_EMPLOYEE!r}."
            )
        entry = entries[0]
        day = dt.date.fromisoformat(entry["work_date"][:10])
        monday = (day - dt.timedelta(days=day.weekday())).isoformat()
        settled = db.execute(
            "SELECT paid_at FROM payments WHERE employee = ? AND period_key = ?",
            (STALE_EMPLOYEE, monday),
        ).fetchone()

        print(f"Будет удалён рейс №{TRIP_ID}: {trip['boat']}, {trip['trip_date']} {trip['trip_time']}, "
              f"выручка {trip['revenue']}")
        print(f"  и запись зарплаты #{entry['id']}: {entry['employee']}, "
              f"{entry['quantity']} ч × {entry['rate']} = {entry['amount']} ₽")
        print(f"Карточка №{CARD_ID} будет отвязана от рейса; автозакрытие создаст верный рейс "
              f"({CARD_BOAT}, {CARD_CAPTAIN}, выручка {card['revenue']}).")
        if not apply:
            print("\nПробный прогон — ничего не изменено. Для исправления добавьте --apply.")
            return 0

        application_module._delete_trip_data(db, TRIP_ID)
        db.execute(
            "UPDATE schedule_items SET accounting_trip_id = NULL, updated_at = ? WHERE id = ?",
            (dt.datetime.now().strftime("%Y-%m-%d %H:%M"), CARD_ID),
        )
        db.commit()
        if settled is not None:
            print(
                f"\nНеделя {monday} у {STALE_EMPLOYEE} отмечена оплаченной ({settled['paid_at']}) — "
                f"отметка оставлена как есть, в программе убрано начисление {entry['amount']} ₽."
            )
        print(f"\nГотово. Рейс №{TRIP_ID} удалён, карточка №{CARD_ID} отвязана.")
        if not close_now:
            print("Автозакрытие подхватит её при ближайшем запуске по расписанию (cron); проверка:\n"
                  "  python3 scripts/diagnose_schedule_item_closing.py 1936")
            return 0

        stats = schedule_services.auto_close_schedule_items(
            db, application_module._create_trip_from_schedule_payload,
            payroll_rates_repository.get_excursion_role_rate,
            apply_minimum_shift=application_module._apply_schedule_minimum_shift,
        )
        print(f"Автозакрытие: закрыто {stats['closed']}, на проверку {stats['needs_review']}, "
              f"пропущено {stats['skipped']}.")
        for line in stats["skipped_details"]:
            print("   пропущено:", line)
        card = db.execute("SELECT accounting_trip_id FROM schedule_items WHERE id = ?", (CARD_ID,)).fetchone()
        if card["accounting_trip_id"] is None:
            print(f"Карточка №{CARD_ID} НЕ закрыта — причина в списке пропущенных выше.")
            return 1
        new_trip = db.execute("SELECT * FROM trips WHERE id = ?", (card["accounting_trip_id"],)).fetchone()
        print(f"Новый рейс №{new_trip['id']}: {new_trip['boat']}, {new_trip['trip_date']} "
              f"{new_trip['trip_time']}, выручка {new_trip['revenue']}, "
              f"needs_review={new_trip['needs_review']}")
        for e in db.execute(
            "SELECT e.* FROM trip_labor tl JOIN entries e ON e.id = tl.entry_id WHERE tl.trip_id = ?",
            (new_trip["id"],),
        ).fetchall():
            print(f"   начисление: {e['employee']} | {e['quantity']} ч × {e['rate']} = {e['amount']} ₽ "
                  f"| {e['work_date']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

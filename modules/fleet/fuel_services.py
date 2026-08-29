"""Fuel-ledger rules shared by admin, captain and cron interfaces."""

import datetime as dt
import math
import uuid

from . import fuel_repository as repository
from .constants import (
    BOAT_COLORS,
    FUEL_CONFIG,
    YCLIENTS_BLOCKED_SHIFT_COLOR,
    YCLIENTS_CANCELLED_COLOR,
)


TRANSACTION_LABELS = {
    "calibration": "Заправка до полного",
    "refill": "Заправка",
    "reserve_refill": "Заправка канистр",
    "reserve_transfer": "Перелив из резерва в бак",
    "group_consumption": "Групповой рейс",
    "individual_consumption": "Индивидуальный рейс",
}

MAX_RESERVE_OPERATION_LITERS = 1000.0


def current_datetime():
    return dt.datetime.now()


def format_timestamp(value):
    return value.strftime("%Y-%m-%d %H:%M")


def _parse_local_datetime(raw_value):
    raw = (raw_value or "").strip().replace("T", " ")
    try:
        value = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is not None:
        # YCLIENTS returns the branch-local wall clock with an offset. The
        # rest of this application stores local timestamps without offsets,
        # so preserve that clock time instead of converting through the
        # hosting server's timezone (which may be UTC on production).
        value = value.replace(tzinfo=None)
    return value.replace(second=0, microsecond=0)


def _parse_positive_liters(raw_value):
    try:
        liters = float(str(raw_value or "").strip().replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(liters) or liters <= 0:
        return None
    return round(liters, 2)


def fuel_summary(db, boat, history_limit=30):
    config = FUEL_CONFIG.get(boat)
    state = repository.get_state(db, boat)
    activated = bool(state and state["activated_at"])
    balance = round(repository.balance_at(db, boat), 2) if activated else None
    reserve_balance = round(repository.reserve_balance_at(db, boat), 2)
    capacity = config["capacity_liters"] if config else None
    percent = 0.0
    status = "inactive"
    trips_remaining = None
    if activated and capacity:
        percent = max(0.0, min(100.0, round(balance / capacity * 100, 1)))
        if percent <= 15:
            status = "critical"
        elif percent <= 30:
            status = "low"
        else:
            status = "normal"
        trips_remaining = max(0, math.floor(max(balance, 0) / config["group_trip_liters"]))

    transactions = []
    for row in repository.list_transactions(db, boat, history_limit):
        item = dict(row)
        item["label"] = TRANSACTION_LABELS.get(item["kind"], item["kind"])
        transactions.append(item)

    return {
        "boat": boat,
        "configured": config is not None,
        "capacity_liters": capacity,
        "group_trip_liters": config["group_trip_liters"] if config else None,
        "activated": activated,
        "activated_at": state["activated_at"] if state else None,
        "last_synced_at": state["last_synced_at"] if state else None,
        "balance_liters": balance,
        "reserve_liters": reserve_balance,
        "total_liters": (
            round(balance + reserve_balance, 2) if balance is not None else None
        ),
        "gauge_liters": max(0.0, min(capacity, balance)) if activated and capacity else None,
        "percent": percent,
        "status": status,
        "trips_remaining": trips_remaining,
        "pending_trips": [dict(row) for row in repository.list_pending_trip_events(db, boat)],
        "transactions": transactions,
        "now_local": current_datetime().strftime("%Y-%m-%dT%H:%M"),
    }


def record_refill(
    db,
    boat,
    raw_liters,
    raw_occurred_at,
    fill_to_full,
    actor_role,
    actor_name,
    operation="tank",
):
    config = FUEL_CONFIG.get(boat)
    if config is None:
        return False, "Для этого катера не настроен топливный бак."

    liters = _parse_positive_liters(raw_liters)
    if liters is None:
        return False, "Укажите объём заправки больше нуля."
    if operation not in {"tank", "reserve", "reserve_to_tank"}:
        operation = "tank"
    if operation == "tank" and liters > config["capacity_liters"]:
        return False, "Объём заправки превышает ёмкость бака."
    if operation == "reserve" and liters > MAX_RESERVE_OPERATION_LITERS:
        return False, "Объём заправки резервных канистр слишком большой."

    occurred = _parse_local_datetime(raw_occurred_at)
    now = current_datetime().replace(second=0, microsecond=0)
    if occurred is None:
        return False, "Укажите дату и время заправки."
    if occurred > now + dt.timedelta(minutes=5):
        return False, "Дата заправки не может быть в будущем."

    state = repository.get_state(db, boat)
    activated_at = _parse_local_datetime(state["activated_at"]) if state else None
    if operation == "tank" and activated_at is None and not fill_to_full:
        return False, "Сначала отметьте первую заправку до полного бака."
    if operation in {"tank", "reserve_to_tank"} and activated_at is not None and occurred < activated_at:
        return False, "Заправка не может быть раньше запуска учёта топлива."
    if operation == "reserve_to_tank" and activated_at is None:
        return False, "Сначала запустите учёт топлива полной заправкой бака."

    occurred_at = format_timestamp(occurred)
    created_at = format_timestamp(now)
    balance_before = repository.balance_at(db, boat, occurred_at) if activated_at else 0.0
    reserve_before = repository.reserve_balance_at(db, boat, occurred_at)
    capacity = config["capacity_liters"]

    if operation == "reserve":
        source_ref = f"manual:{uuid.uuid4().hex}"
        with db:
            repository.add_transaction(
                db,
                boat,
                "reserve_refill",
                0,
                liters,
                occurred_at,
                source_ref,
                "Заправка резервных канистр",
                actor_role,
                actor_name,
                created_at,
                reserve_delta=liters,
            )
        return True, f"В резерв катера «{boat}» добавлено {liters:g} л."

    if operation == "reserve_to_tank":
        if reserve_before + 0.01 < liters:
            return False, f"В резерве только {max(0, reserve_before):g} л."
        free = max(0.0, round(capacity - balance_before, 2))
        if liters > free + 0.01:
            return False, f"По расчёту в бак помещается не больше {free:g} л."
        source_ref = f"manual:{uuid.uuid4().hex}"
        with db:
            repository.add_transaction(
                db,
                boat,
                "reserve_transfer",
                liters,
                liters,
                occurred_at,
                source_ref,
                "Перелив из резервных канистр",
                actor_role,
                actor_name,
                created_at,
                reserve_delta=-liters,
            )
        return True, f"Из резерва в бак катера «{boat}» перелито {liters:g} л."

    if fill_to_full:
        delta = round(capacity - balance_before, 2)
        kind = "calibration"
        label = "Запуск учёта: полный бак" if activated_at is None else "Заправка до полного"
    else:
        if balance_before + liters > capacity + 0.01:
            free = max(0.0, round(capacity - balance_before, 2))
            return False, (
                f"По расчёту в бак помещается не больше {free:g} л. "
                "Если бак заправлен полностью, отметьте «До полного»."
            )
        delta = liters
        kind = "refill"
        label = "Заправка"

    source_ref = f"manual:{uuid.uuid4().hex}"
    with db:
        repository.add_transaction(
            db,
            boat,
            kind,
            delta,
            liters,
            occurred_at,
            source_ref,
            label,
            actor_role,
            actor_name,
            created_at,
        )
        if activated_at is None:
            repository.activate_state(
                db, boat, occurred_at, actor_role, actor_name, created_at
            )

    if activated_at is None:
        return True, f"Учёт топлива запущен: полный бак {capacity:g} л."
    if fill_to_full:
        return True, f"Уровень катера «{boat}» откалиброван до {capacity:g} л."
    return True, f"Заправка {liters:g} л добавлена в журнал катера «{boat}»."


def record_individual_consumption(db, boat, event_id, raw_liters, actor_role, actor_name):
    liters = _parse_positive_liters(raw_liters)
    if liters is None:
        return False, "Укажите расход больше нуля."

    config = FUEL_CONFIG.get(boat)
    if config is None or liters > config["capacity_liters"]:
        return False, "Расход превышает ёмкость бака."

    event = repository.get_trip_event(db, event_id, boat)
    if event is None or event["trip_kind"] != "individual":
        return False, "Индивидуальный рейс не найден."
    if event["status"] != "pending":
        return False, "Расход по этому рейсу уже указан."

    now = format_timestamp(current_datetime())
    source_ref = f"fuel-trip:{event['source_ref']}"
    with db:
        transaction_id = repository.add_transaction(
            db,
            boat,
            "individual_consumption",
            -liters,
            liters,
            event["ended_at"],
            source_ref,
            event["service_title"] or "Индивидуальный рейс",
            actor_role,
            actor_name,
            now,
        )
        repository.mark_trip_consumed(db, event["id"], liters, transaction_id)
    return True, f"Расход {liters:g} л по индивидуальному рейсу сохранён."


def delete_transaction(db, boat, transaction_id, actor_name):
    transaction = repository.get_transaction(db, transaction_id, boat)
    if transaction is None or transaction["deleted_at"]:
        return False, "Запись журнала топлива не найдена."

    state = repository.get_state(db, boat)
    is_initial_calibration = (
        transaction["kind"] == "calibration"
        and state is not None
        and state["activated_at"] == transaction["occurred_at"]
    )
    active_count = repository.count_active_transactions(db, boat)
    if is_initial_calibration and active_count > 1:
        return False, (
            "Начальную заправку нельзя удалить, пока после неё есть другие операции. "
            "Сначала удалите более поздние записи журнала."
        )
    reserve_after_delete = (
        repository.reserve_balance_at(db, boat) - transaction["reserve_delta"]
    )
    if reserve_after_delete < -0.01:
        return False, (
            "Эту заправку канистр нельзя удалить: резерв станет отрицательным. "
            "Сначала удалите более поздние переливы в бак."
        )

    deleted_at = format_timestamp(current_datetime())
    with db:
        deleted = repository.soft_delete_transaction(
            db, transaction_id, boat, deleted_at, actor_name
        )
        if not deleted:
            return False, "Запись журнала топлива уже удалена."
        if is_initial_calibration:
            repository.deactivate_state(db, boat, deleted_at)
            repository.delete_trip_events(db, boat)

    label = TRANSACTION_LABELS.get(transaction["kind"], "Операция")
    if is_initial_calibration:
        return True, "Начальная заправка удалена. Учёт топлива остановлен."
    return True, f"Запись «{label}» удалена, остаток топлива пересчитан."


def _normalise_color(value):
    return (value or "").strip().lower().lstrip("#")


def _record_color(record):
    return _normalise_color(record.get("custom_color") or record.get("color"))


def _is_no_show(record):
    values = (record.get("attendance"), record.get("visit_attendance"))
    return any(str(value).strip() == "-1" for value in values if value is not None)


def _record_start(record):
    return _parse_local_datetime(record.get("datetime") or record.get("date"))


def _record_duration_seconds(record):
    try:
        return max(0, int(record.get("seance_length") or record.get("length") or 0))
    except (TypeError, ValueError):
        return 0


def _slot_key(record):
    activity_id = record.get("activity_id")
    if activity_id:
        return f"activity:{activity_id}"
    started = _record_start(record)
    color = _record_color(record)
    if started and color:
        return f"slot:{color}:{started.strftime('%Y-%m-%dT%H:%M')}"
    return f"record:{record.get('id')}"


def _activity_color(source_ref, activity_colors):
    if not source_ref.startswith("activity:"):
        return ""
    activity_raw = source_ref.split(":", 1)[1]
    color = activity_colors.get(activity_raw)
    if color is None and activity_raw.isdigit():
        color = activity_colors.get(int(activity_raw))
    return _normalise_color(color)


def _boat_for_group(source_ref, records, activity_colors):
    if source_ref.startswith("activity:"):
        normalised = _activity_color(source_ref, activity_colors)
        return next(
            (boat for raw_color, boat in BOAT_COLORS.items() if _normalise_color(raw_color) == normalised),
            None,
        )

    for record in records:
        color = _record_color(record)
        for raw_color, boat in BOAT_COLORS.items():
            if _normalise_color(raw_color) == color:
                return boat
    return None


def remove_cancelled_yclients_trips(db, source_refs):
    """Remove fuel events/debits for cancelled YCLIENTS source references."""
    return sum(
        repository.delete_yclients_trip_by_source(db, source_ref)
        for source_ref in set(source_refs)
    )


def sync_yclients_records(db, records, activity_colors=None, now=None):
    """Create idempotent fuel events from completed, non-cancelled records."""
    activity_colors = activity_colors or {}
    now = (now or current_datetime()).replace(second=0, microsecond=0)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    synced_at = format_timestamp(now)

    cancelled_source_refs = {
        _slot_key(record)
        for record in records
        if (
            _record_color(record) == YCLIENTS_CANCELLED_COLOR
            or _activity_color(_slot_key(record), activity_colors)
            == YCLIENTS_CANCELLED_COLOR
        )
    }
    cancelled = remove_cancelled_yclients_trips(
        db, cancelled_source_refs
    )

    groups = {}
    for record in records:
        if record.get("deleted") or _is_no_show(record):
            continue
        source_ref = _slot_key(record)
        if (
            _record_color(record) == YCLIENTS_BLOCKED_SHIFT_COLOR
            or source_ref in cancelled_source_refs
        ):
            continue
        groups.setdefault(source_ref, []).append(record)

    stats = {
        "automatic": 0,
        "pending": 0,
        "skipped": 0,
        "cancelled": cancelled,
    }
    for source_ref, grouped_records in groups.items():
        boat = _boat_for_group(source_ref, grouped_records, activity_colors)
        config = FUEL_CONFIG.get(boat)
        if config is None:
            stats["skipped"] += 1
            continue

        state = repository.get_state(db, boat)
        activated_at = _parse_local_datetime(state["activated_at"]) if state else None
        if activated_at is None:
            continue

        starts = [value for value in (_record_start(item) for item in grouped_records) if value]
        duration_seconds = max(
            (_record_duration_seconds(item) for item in grouped_records),
            default=0,
        )
        if not starts or duration_seconds <= 0:
            stats["skipped"] += 1
            continue
        started = min(starts)
        ended = started + dt.timedelta(seconds=duration_seconds)
        if started < activated_at or ended > now:
            continue

        service = next(
            (
                service
                for item in grouped_records
                for service in (item.get("services") or [])
                if service
            ),
            None,
        )
        service_title = (service or {}).get("title") or "Рейс YCLIENTS"
        trip_kind = "group" if source_ref.startswith("activity:") else "individual"
        event = repository.upsert_trip_event(
            db,
            source_ref,
            boat,
            trip_kind,
            format_timestamp(started),
            format_timestamp(ended),
            service_title,
            synced_at,
        )
        if trip_kind == "individual":
            if event["status"] == "pending":
                stats["pending"] += 1
            continue

        transaction_source = f"fuel-trip:{source_ref}"
        if repository.get_transaction_by_source(db, transaction_source) is not None:
            continue
        liters = config["group_trip_liters"]
        transaction_id = repository.add_transaction(
            db,
            boat,
            "group_consumption",
            -liters,
            liters,
            format_timestamp(ended),
            transaction_source,
            service_title,
            "system",
            "YCLIENTS",
            synced_at,
        )
        repository.mark_trip_consumed(db, event["id"], liters, transaction_id)
        stats["automatic"] += 1

    for boat in FUEL_CONFIG:
        state = repository.get_state(db, boat)
        if state and state["activated_at"]:
            repository.set_last_synced_at(db, boat, synced_at)
    db.commit()
    return stats

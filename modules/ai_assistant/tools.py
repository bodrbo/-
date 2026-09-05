"""Role-aware, read-only tools available to the language model."""

import datetime as dt

from .constants import GUIDE_TOPICS
from .data_catalog import (
    DATASET_IDS,
    catalog_for_user,
    visible_chart_subjects,
    visible_dataset_ids,
)
from .knowledge import guide_for


TUNING_STATUS_LABELS = {
    "new_request": "Новая заявка",
    "estimate": "Предварительный расчёт",
    "in_progress": "В работе",
    "qc": "Проходит независимый контроль качества",
    "done": "Выполнен",
    "cancelled": "Отменён",
}
TUNING_SALE_CHANNEL_LABELS = {
    "direct": "Напрямую",
    "aggregator": "Через агрегатора/агента",
    "mixed": "Смешанно / другое",
}
CLIENT_STATUS_LABELS = {
    "satisfied": "Довольные",
    "neutral": "Нейтральные",
    "dissatisfied": "Недовольные",
    "blacklisted": "Чёрный список",
}
SCHEDULE_KIND_LABELS = {
    "booking": "Аренда катера",
    "event": "Групповая экскурсия",
}
EQUIPMENT_TYPE_LABELS = {"boat": "Лодки", "motor": "Моторы"}
MONTH_NAMES_SHORT = (
    "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)


class ToolAccessError(ValueError):
    pass


def _is_admin(user):
    return user.get("owner_type") == "admin"


def _positions(user):
    return {str(value).casefold() for value in user.get("positions", [])}


def _is_manager(user):
    return "менеджер по работе с клиентами" in _positions(user)


def _is_captain(user):
    return bool(
        _positions(user)
        & {"капитан", "гид-капитан", "капитан-механик"}
    )


def _date_period(arguments, default_days=7, maximum_days=366):
    today = dt.date.today()
    default_from = today - dt.timedelta(days=default_days - 1)
    try:
        date_from = dt.date.fromisoformat(str(arguments.get("date_from") or default_from))
        date_to = dt.date.fromisoformat(str(arguments.get("date_to") or today))
    except ValueError as error:
        raise ValueError("Даты должны быть в формате YYYY-MM-DD.") from error
    if date_to < date_from:
        raise ValueError("Дата окончания не может быть раньше даты начала.")
    if (date_to - date_from).days > maximum_days:
        raise ValueError(f"Период не должен превышать {maximum_days + 1} дней.")
    return date_from, date_to


def _rows_to_counts(rows, key="label"):
    return {str(row[key] or "Не указано"): int(row["total"] or 0) for row in rows}


def _money(value):
    return round(float(value or 0), 2)


def _schedule_summary(db, arguments):
    date_from, date_to = _date_period(arguments)
    boat = str(arguments.get("boat") or "").strip()
    params = [date_from.isoformat(), (date_to + dt.timedelta(days=1)).isoformat()]
    where = (
        "deleted_at IS NULL AND starts_at >= ? AND starts_at < ?"
    )
    if boat:
        where += " AND boat = ?"
        params.append(boat)
    totals = db.execute(
        f"SELECT COUNT(*) AS trips, COALESCE(SUM(revenue), 0) AS revenue, "
        f"COALESCE(SUM(participants_count), 0) AS guests "
        f"FROM schedule_items WHERE {where}",
        params,
    ).fetchone()
    by_boat = db.execute(
        f"SELECT boat AS label, COUNT(*) AS total FROM schedule_items "
        f"WHERE {where} GROUP BY boat ORDER BY total DESC",
        params,
    ).fetchall()
    by_service = db.execute(
        f"SELECT service_name AS label, COUNT(*) AS total FROM schedule_items "
        f"WHERE {where} GROUP BY service_name ORDER BY total DESC LIMIT 12",
        params,
    ).fetchall()
    by_kind = db.execute(
        f"SELECT kind AS label, COUNT(*) AS total FROM schedule_items "
        f"WHERE {where} GROUP BY kind ORDER BY total DESC",
        params,
    ).fetchall()
    unassigned = db.execute(
        f"SELECT COUNT(*) AS total FROM schedule_items i WHERE {where} "
        "AND NOT EXISTS (SELECT 1 FROM schedule_assignments a "
        "WHERE a.schedule_item_id = i.id)",
        params,
    ).fetchone()["total"]
    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "boat_filter": boat or None,
        "trips": int(totals["trips"] or 0),
        "planned_revenue_rub": _money(totals["revenue"]),
        "guests": int(totals["guests"] or 0),
        "unassigned_trips": int(unassigned or 0),
        "by_boat": _rows_to_counts(by_boat),
        "by_service": _rows_to_counts(by_service),
        "by_kind": _rows_to_counts(by_kind),
    }


def _fleet_status(db, arguments, boats):
    requested_boat = str(arguments.get("boat") or "").strip()
    known_boats = [boat["name"] for boat in boats]
    if requested_boat and requested_boat not in known_boats:
        raise ValueError("Такого катера нет в справочнике флота.")
    selected = [requested_boat] if requested_boat else known_boats
    result = []
    for boat in selected:
        state = db.execute(
            "SELECT activated_at, last_synced_at FROM boat_fuel_state WHERE boat = ?",
            (boat,),
        ).fetchone()
        fuel = db.execute(
            "SELECT COALESCE(SUM(liters_delta), 0) AS tank, "
            "COALESCE(SUM(reserve_delta), 0) AS reserve "
            "FROM boat_fuel_transactions WHERE boat = ? AND deleted_at IS NULL",
            (boat,),
        ).fetchone()
        defects = db.execute(
            "SELECT status AS label, COUNT(*) AS total FROM boat_defects "
            "WHERE boat = ? AND status != 'resolved' GROUP BY status",
            (boat,),
        ).fetchall()
        result.append({
            "boat": boat,
            "fuel_tracking_active": bool(state and state["activated_at"]),
            "fuel_activated_at": state["activated_at"] if state else None,
            "fuel_last_synced_at": state["last_synced_at"] if state else None,
            "tank_liters": _money(fuel["tank"]),
            "reserve_liters": _money(fuel["reserve"]),
            "current_defects": _rows_to_counts(defects),
            "current_defects_total": sum(int(row["total"] or 0) for row in defects),
        })
    return {"boats": result}


def _tuning_summary(db, arguments):
    date_from, date_to = _date_period(arguments, default_days=30)
    status = str(arguments.get("status") or "").strip()
    params = [date_from.isoformat(), date_to.isoformat()]
    where = "o.order_date >= ? AND o.order_date <= ?"
    if status:
        where += " AND o.status = ?"
        params.append(status)
    totals = db.execute(
        "SELECT COUNT(o.id) AS orders_count, "
        "COALESCE(SUM(o.total), 0) AS total, "
        "COALESCE(SUM(COALESCE(p.paid_total, 0)), 0) AS paid_total, "
        "COALESCE(SUM(MAX(o.total - COALESCE(p.paid_total, 0), 0)), 0) AS outstanding_total "
        "FROM tuning_orders o LEFT JOIN ("
        " SELECT order_id, SUM(amount) AS paid_total FROM tuning_payments GROUP BY order_id"
        f") p ON p.order_id = o.id WHERE {where}",
        params,
    ).fetchone()
    statuses = db.execute(
        "SELECT o.status AS label, COUNT(*) AS total, "
        "COALESCE(SUM(o.total), 0) AS amount FROM tuning_orders o "
        f"WHERE {where} GROUP BY o.status ORDER BY amount DESC",
        params,
    ).fetchall()
    equipment = db.execute(
        "SELECT o.equipment_type AS label, COUNT(*) AS total FROM tuning_orders o "
        f"WHERE {where} GROUP BY o.equipment_type ORDER BY total DESC",
        params,
    ).fetchall()
    channels = db.execute(
        "SELECT o.sale_channel AS label, COUNT(*) AS total, "
        "COALESCE(SUM(o.total), 0) AS amount FROM tuning_orders o "
        f"WHERE {where} GROUP BY o.sale_channel ORDER BY amount DESC",
        params,
    ).fetchall()
    payment_params = [date_from.isoformat(), date_to.isoformat()]
    payment_where = (
        "substr(tp.paid_at, 1, 10) >= ? AND substr(tp.paid_at, 1, 10) <= ?"
    )
    if status:
        payment_where += " AND o.status = ?"
        payment_params.append(status)
    payments = db.execute(
        "SELECT COUNT(*) AS payments_count, COALESCE(SUM(amount), 0) AS amount "
        "FROM tuning_payments tp JOIN tuning_orders o ON o.id = tp.order_id "
        f"WHERE {payment_where}",
        payment_params,
    ).fetchone()
    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "date_basis": "order_date",
        "date_basis_note": "Заказы отобраны по дате заказа из интерфейса, а не по технической дате добавления в базу.",
        "status_filter": status or None,
        "orders": int(totals["orders_count"] or 0),
        "orders_total_rub": _money(totals["total"]),
        "payments_for_selected_orders_rub": _money(totals["paid_total"]),
        "current_outstanding_for_selected_orders_rub": _money(
            totals["outstanding_total"]
        ),
        "payments_received_in_period": int(payments["payments_count"] or 0),
        "payments_received_in_period_rub": _money(payments["amount"]),
        "by_status": _rows_to_counts(statuses),
        "status_breakdown": [
            {
                "status": str(row["label"] or "Не указано"),
                "status_label": TUNING_STATUS_LABELS.get(
                    str(row["label"] or ""), "Не указано"
                ),
                "orders": int(row["total"] or 0),
                "orders_total_rub": _money(row["amount"]),
            }
            for row in statuses
        ],
        "sale_channel_breakdown": [
            {
                "sale_channel": str(row["label"] or "Не указано"),
                "sale_channel_label": TUNING_SALE_CHANNEL_LABELS.get(
                    str(row["label"] or ""), "Не указано"
                ),
                "orders": int(row["total"] or 0),
                "orders_total_rub": _money(row["amount"]),
            }
            for row in channels
        ],
        "by_equipment_type": _rows_to_counts(equipment),
    }


def _clients_summary(db, arguments, user):
    segment = str(arguments.get("segment") or "excursion").strip()
    if segment not in ("excursion", "tuning"):
        raise ValueError("Сегмент должен быть excursion или tuning.")
    if not _is_admin(user) and segment != "excursion":
        raise ToolAccessError("Сотруднику доступна только экскурсионная клиентская база.")
    total = db.execute(
        "SELECT COUNT(*) AS total FROM client_segments WHERE segment = ?",
        (segment,),
    ).fetchone()["total"]
    statuses = db.execute(
        "SELECT c.status AS label, COUNT(*) AS total FROM clients c "
        "JOIN client_segments s ON s.client_id = c.id "
        "WHERE s.segment = ? GROUP BY c.status ORDER BY total DESC",
        (segment,),
    ).fetchall()
    channels = db.execute(
        "SELECT c.acquisition_channel AS label, COUNT(*) AS total FROM clients c "
        "JOIN client_segments s ON s.client_id = c.id "
        "WHERE s.segment = ? GROUP BY c.acquisition_channel ORDER BY total DESC",
        (segment,),
    ).fetchall()
    return {
        "segment": segment,
        "clients": int(total or 0),
        "by_status": _rows_to_counts(statuses),
        "by_acquisition_channel": _rows_to_counts(channels),
        "privacy_note": "Имена, телефоны и другие персональные данные не переданы.",
    }


def _payroll_summary(db, arguments, user):
    date_from, date_to = _date_period(arguments, default_days=7)
    requested_employee = str(arguments.get("employee_name") or "").strip()
    employee_name = requested_employee
    if not _is_admin(user):
        employee_name = user["name"]
    params = [date_from.isoformat(), date_to.isoformat()]
    where = "work_date >= ? AND work_date <= ?"
    if employee_name:
        where += " AND employee = ?"
        params.append(employee_name)
    totals = db.execute(
        f"SELECT COUNT(*) AS entries_count, COALESCE(SUM(amount), 0) AS amount "
        f"FROM entries WHERE {where}",
        params,
    ).fetchone()
    by_work_type = db.execute(
        f"SELECT work_type AS label, COUNT(*) AS total, "
        f"COALESCE(SUM(amount), 0) AS amount FROM entries WHERE {where} "
        "GROUP BY work_type ORDER BY amount DESC LIMIT 20",
        params,
    ).fetchall()
    return {
        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
        "employee": employee_name or "Все сотрудники",
        "entries": int(totals["entries_count"] or 0),
        "amount_rub": _money(totals["amount"]),
        "by_work_type": [
            {
                "work_type": row["label"],
                "entries": int(row["total"] or 0),
                "amount_rub": _money(row["amount"]),
            }
            for row in by_work_type
        ],
    }


def _tasks_summary(db, arguments, user):
    requested_employee = str(arguments.get("employee_name") or "").strip()
    employee_name = requested_employee if _is_admin(user) else user["name"]
    params = []
    defect_where = "da.assignment_status IN ('pending', 'accepted')"
    tuning_where = "ta.assignment_status IN ('pending', 'accepted')"
    if employee_name:
        defect_where += " AND da.employee_name = ?"
        tuning_where += " AND ta.employee_name = ?"
        params.append(employee_name)
    defect_rows = db.execute(
        "SELECT da.assignment_status AS status, COUNT(*) AS total "
        "FROM defect_assignments da WHERE " + defect_where + " GROUP BY da.assignment_status",
        params,
    ).fetchall()
    tuning_rows = db.execute(
        "SELECT ta.assignment_status AS status, COUNT(*) AS total "
        "FROM tuning_item_assignments ta WHERE " + tuning_where + " GROUP BY ta.assignment_status",
        params,
    ).fetchall()
    return {
        "employee": employee_name or "Все сотрудники",
        "fleet_tasks": {row["status"]: int(row["total"] or 0) for row in defect_rows},
        "tuning_tasks": {row["status"]: int(row["total"] or 0) for row in tuning_rows},
    }


def _employees_directory(db, arguments, user):
    if not _is_admin(user):
        raise ToolAccessError("Справочник сотрудников доступен только администратору.")

    activity = str(arguments.get("activity") or "active").strip()
    account_state = str(arguments.get("account_state") or "all").strip()
    telegram_state = str(arguments.get("telegram_state") or "all").strip()
    position = str(arguments.get("position") or "").strip()
    include_names = arguments.get("include_names", True)
    limit = arguments.get("limit", 100)
    if activity not in ("active", "deleted", "all"):
        raise ValueError("Активность должна быть active, deleted или all.")
    if account_state not in ("all", "created", "missing"):
        raise ValueError("Состояние кабинета должно быть all, created или missing.")
    if telegram_state not in ("all", "linked", "missing"):
        raise ValueError("Состояние Telegram должно быть all, linked или missing.")
    if len(position) > 80:
        raise ValueError("Название должности слишком длинное.")
    if not isinstance(include_names, bool):
        raise ValueError("include_names должен быть логическим значением.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit должен быть целым числом от 1 до 100.")

    rows = db.execute(
        "SELECT e.id, e.name, e.deleted_at, "
        "EXISTS(SELECT 1 FROM team_accounts ta WHERE ta.employee_id = e.id) "
        "AS account_created, "
        "(EXISTS(SELECT 1 FROM employee_telegram_accounts eta "
        "WHERE eta.employee_id = e.id) OR EXISTS("
        "SELECT 1 FROM team_accounts legacy WHERE legacy.employee_id = e.id "
        "AND legacy.telegram_chat_id IS NOT NULL AND legacy.telegram_chat_id != ''"
        ")) AS telegram_linked "
        "FROM employees e ORDER BY e.name"
    ).fetchall()
    position_rows = db.execute(
        "SELECT employee_id, position FROM employee_positions "
        "ORDER BY position, employee_id"
    ).fetchall()
    positions_by_employee = {}
    for row in position_rows:
        positions_by_employee.setdefault(row["employee_id"], []).append(row["position"])

    selected = []
    requested_position = position.casefold()
    for row in rows:
        active = row["deleted_at"] is None
        account_created = bool(row["account_created"])
        telegram_linked = bool(row["telegram_linked"])
        positions = positions_by_employee.get(row["id"], [])
        if activity == "active" and not active:
            continue
        if activity == "deleted" and active:
            continue
        if account_state == "created" and not account_created:
            continue
        if account_state == "missing" and account_created:
            continue
        if telegram_state == "linked" and not telegram_linked:
            continue
        if telegram_state == "missing" and telegram_linked:
            continue
        if requested_position and requested_position not in {
            value.casefold() for value in positions
        }:
            continue
        selected.append({
            "name": row["name"],
            "positions": positions,
            "active": active,
            "account_created": account_created,
            "telegram_linked": telegram_linked,
        })

    by_position = {}
    for employee in selected:
        for value in employee["positions"]:
            by_position[value] = by_position.get(value, 0) + 1
    result = {
        "filters": {
            "position": position or None,
            "activity": activity,
            "account_state": account_state,
            "telegram_state": telegram_state,
        },
        "employees_total": len(selected),
        "active_employees": sum(1 for item in selected if item["active"]),
        "deleted_employees": sum(1 for item in selected if not item["active"]),
        "accounts_created": sum(1 for item in selected if item["account_created"]),
        "accounts_missing": sum(1 for item in selected if not item["account_created"]),
        "telegram_linked": sum(1 for item in selected if item["telegram_linked"]),
        "telegram_missing": sum(1 for item in selected if not item["telegram_linked"]),
        "by_position": dict(sorted(by_position.items())),
        "privacy_note": (
            "Доступ администратора. Переданы только имена, должности и логические "
            "признаки кабинета/Telegram; логины, пароли, хеши и Telegram ID исключены."
        ),
    }
    if include_names:
        result["directory"] = selected[:limit]
        result["directory_truncated"] = len(selected) > limit
    return result


def _business_overview(db, arguments, user, boats):
    if not _is_admin(user):
        raise ToolAccessError("Общий обзор бизнеса доступен только администратору.")
    date_from, date_to = _date_period(arguments, default_days=7)
    period_arguments = {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }
    return {
        "period": period_arguments,
        "schedule": _schedule_summary(db, period_arguments),
        "tuning": _tuning_summary(db, period_arguments),
        "payroll": _payroll_summary(db, period_arguments, user),
        "fleet": _fleet_status(db, {}, boats),
    }


def _chart_period_label(date_from, date_to):
    return "{} — {}".format(
        date_from.strftime("%d.%m.%Y"), date_to.strftime("%d.%m.%Y")
    )


def _chart_category_label(value, group_by, labels=None):
    raw = str(value or "Не указано")
    if labels:
        return labels.get(raw, raw or "Не указано")
    if group_by == "day":
        try:
            return dt.date.fromisoformat(raw).strftime("%d.%m")
        except ValueError:
            return raw
    if group_by == "month":
        try:
            year, month = (int(part) for part in raw.split("-", 1))
            return f"{MONTH_NAMES_SHORT[month - 1]} {year}"
        except (ValueError, IndexError):
            return raw
    return raw or "Не указано"


def _bar_visualization(title, subtitle, dataset_label, value_format, rows, group_by,
                       labels=None):
    return {
        "type": "bar",
        "title": title,
        "subtitle": subtitle,
        "value_format": value_format,
        "labels": [
            _chart_category_label(row["category"], group_by, labels) for row in rows
        ],
        "datasets": [{
            "label": dataset_label,
            "data": [_money(row["value"]) for row in rows],
        }],
    }


def _grouped_chart_query(db, table, date_column, date_from, date_to, group_expression,
                         value_expression, temporal, extra_where="", extra_params=None):
    params = [date_from.isoformat(), date_to.isoformat()]
    where = f"{date_column} >= ? AND {date_column} <= ?"
    if extra_where:
        where += " AND " + extra_where
        params.extend(extra_params or [])
    ordering = "category" if temporal else "value DESC, category"
    return db.execute(
        f"SELECT {group_expression} AS category, {value_expression} AS value "
        f"FROM {table} WHERE {where} GROUP BY {group_expression} "
        f"ORDER BY {ordering} LIMIT 60",
        params,
    ).fetchall()


def _tuning_bar_chart(db, arguments):
    date_from, date_to = _date_period(arguments, default_days=30)
    group_by = str(arguments.get("group_by") or "month")
    metric = str(arguments.get("metric") or "amount_rub")
    if group_by == "day" and (date_to - date_from).days >= 60:
        raise ValueError("Для периода больше 60 дней сгруппируйте график по месяцам.")
    groups = {
        "day": ("o.order_date", None),
        "month": ("substr(o.order_date, 1, 7)", None),
        "status": ("o.status", TUNING_STATUS_LABELS),
        "sale_channel": ("o.sale_channel", TUNING_SALE_CHANNEL_LABELS),
        "equipment_type": ("o.equipment_type", EQUIPMENT_TYPE_LABELS),
    }
    metrics = {
        "orders": ("COUNT(o.id)", "Количество заказов", "integer"),
        "amount_rub": ("COALESCE(SUM(o.total), 0)", "Стоимость заказов", "currency"),
    }
    if group_by not in groups or metric not in metrics:
        raise ValueError("Для графика тюнинга выбраны несовместимые параметры.")
    group_expression, labels = groups[group_by]
    value_expression, dataset_label, value_format = metrics[metric]
    rows = _grouped_chart_query(
        db, "tuning_orders o", "o.order_date", date_from, date_to,
        group_expression, value_expression, group_by in ("day", "month"),
    )
    title = "Тюнинг-заказы по " + {
        "day": "дням", "month": "месяцам", "status": "статусам",
        "sale_channel": "каналам продаж", "equipment_type": "типам техники",
    }[group_by]
    return _bar_visualization(
        title, _chart_period_label(date_from, date_to), dataset_label, value_format,
        rows, group_by, labels,
    )


def _schedule_bar_chart(db, arguments):
    date_from, date_to = _date_period(arguments)
    group_by = str(arguments.get("group_by") or "day")
    metric = str(arguments.get("metric") or "trips")
    if group_by == "day" and (date_to - date_from).days >= 60:
        raise ValueError("Для периода больше 60 дней сгруппируйте график по месяцам.")
    groups = {
        "day": ("substr(i.starts_at, 1, 10)", None),
        "month": ("substr(i.starts_at, 1, 7)", None),
        "boat": ("i.boat", None),
        "service": ("i.service_name", None),
        "kind": ("i.kind", SCHEDULE_KIND_LABELS),
    }
    metrics = {
        "trips": ("COUNT(i.id)", "Количество рейсов", "integer"),
        "revenue_rub": ("COALESCE(SUM(i.revenue), 0)", "Плановая выручка", "currency"),
        "guests": ("COALESCE(SUM(i.participants_count), 0)", "Количество гостей", "integer"),
    }
    if group_by not in groups or metric not in metrics:
        raise ValueError("Для графика расписания выбраны несовместимые параметры.")
    group_expression, labels = groups[group_by]
    value_expression, dataset_label, value_format = metrics[metric]
    rows = _grouped_chart_query(
        db, "schedule_items i", "substr(i.starts_at, 1, 10)", date_from, date_to,
        group_expression, value_expression, group_by in ("day", "month"),
        "i.deleted_at IS NULL",
    )
    title = "Расписание по " + {
        "day": "дням", "month": "месяцам", "boat": "катерам",
        "service": "услугам", "kind": "видам рейсов",
    }[group_by]
    return _bar_visualization(
        title, _chart_period_label(date_from, date_to), dataset_label, value_format,
        rows, group_by, labels,
    )


def _payroll_bar_chart(db, arguments, user):
    date_from, date_to = _date_period(arguments)
    group_by = str(arguments.get("group_by") or "day")
    metric = str(arguments.get("metric") or "amount_rub")
    if group_by == "day" and (date_to - date_from).days >= 60:
        raise ValueError("Для периода больше 60 дней сгруппируйте график по месяцам.")
    groups = {
        "day": "e.work_date",
        "month": "substr(e.work_date, 1, 7)",
        "employee": "e.employee",
        "work_type": "e.work_type",
    }
    metrics = {
        "entries": ("COUNT(e.id)", "Количество начислений", "integer"),
        "amount_rub": ("COALESCE(SUM(e.amount), 0)", "Начислено", "currency"),
    }
    if group_by not in groups or metric not in metrics:
        raise ValueError("Для графика зарплат выбраны несовместимые параметры.")
    requested_employee = str(arguments.get("employee_name") or "").strip()
    employee_name = requested_employee if _is_admin(user) else user["name"]
    extra_where = ""
    extra_params = []
    if employee_name:
        extra_where = "e.employee = ?"
        extra_params.append(employee_name)
    value_expression, dataset_label, value_format = metrics[metric]
    rows = _grouped_chart_query(
        db, "entries e", "e.work_date", date_from, date_to, groups[group_by],
        value_expression, group_by in ("day", "month"), extra_where, extra_params,
    )
    title = "Начисления по " + {
        "day": "дням", "month": "месяцам", "employee": "сотрудникам",
        "work_type": "видам работ",
    }[group_by]
    return _bar_visualization(
        title, _chart_period_label(date_from, date_to), dataset_label, value_format,
        rows, group_by,
    )


def _clients_bar_chart(db, arguments, user):
    segment = str(arguments.get("segment") or "excursion")
    if segment not in ("excursion", "tuning"):
        raise ValueError("Сегмент должен быть excursion или tuning.")
    if not _is_admin(user) and (not _is_manager(user) or segment != "excursion"):
        raise ToolAccessError("Этот сегмент клиентской базы недоступен для вашей роли.")
    group_by = str(arguments.get("group_by") or "acquisition_channel")
    groups = {
        "status": ("c.status", CLIENT_STATUS_LABELS),
        "acquisition_channel": ("c.acquisition_channel", None),
    }
    if group_by not in groups or str(arguments.get("metric") or "clients") != "clients":
        raise ValueError("Для графика клиентов выбраны несовместимые параметры.")
    expression, labels = groups[group_by]
    rows = db.execute(
        f"SELECT {expression} AS category, COUNT(DISTINCT c.id) AS value "
        "FROM clients c JOIN client_segments s ON s.client_id = c.id "
        f"WHERE s.segment = ? GROUP BY {expression} ORDER BY value DESC, category LIMIT 60",
        (segment,),
    ).fetchall()
    title = "Клиенты по " + (
        "статусам" if group_by == "status" else "каналам продаж"
    )
    subtitle = "Клиенты тюнинга" if segment == "tuning" else "Клиенты экскурсий"
    return _bar_visualization(
        title, subtitle, "Количество клиентов", "integer", rows, group_by, labels,
    )


def _fleet_bar_chart(db, arguments, user, boats):
    if not (_is_admin(user) or _is_captain(user)):
        raise ToolAccessError("Данные флота недоступны для вашей роли.")
    metric = str(arguments.get("metric") or "tank_liters")
    labels = [boat["name"] for boat in boats]
    if metric in ("tank_liters", "reserve_liters"):
        column = "liters_delta" if metric == "tank_liters" else "reserve_delta"
        values_by_boat = {
            str(row["boat"]): _money(row["value"])
            for row in db.execute(
                f"SELECT boat, COALESCE(SUM({column}), 0) AS value "
                "FROM boat_fuel_transactions WHERE deleted_at IS NULL GROUP BY boat"
            ).fetchall()
        }
        values = [values_by_boat.get(boat, 0) for boat in labels]
        dataset_label = "В баке" if metric == "tank_liters" else "В резерве"
        value_format = "liters"
    elif metric == "defects":
        values_by_boat = {
            str(row["boat"]): int(row["value"] or 0)
            for row in db.execute(
                "SELECT boat, COUNT(*) AS value FROM boat_defects "
                "WHERE status != 'resolved' GROUP BY boat"
            ).fetchall()
        }
        values = [values_by_boat.get(boat, 0) for boat in labels]
        dataset_label = "Текущие неисправности"
        value_format = "integer"
    else:
        raise ValueError("Для графика флота выбран неизвестный показатель.")
    return {
        "type": "bar",
        "title": "Состояние флота",
        "subtitle": "Текущие данные",
        "value_format": value_format,
        "labels": labels,
        "datasets": [{"label": dataset_label, "data": values}],
    }


def _bar_chart(db, arguments, user, boats):
    subject = str(arguments.get("subject") or "").strip()
    if subject == "tuning":
        if not _is_admin(user):
            raise ToolAccessError("Аналитика тюнинга доступна только администратору.")
        chart = _tuning_bar_chart(db, arguments)
    elif subject == "schedule":
        if not (_is_admin(user) or _is_manager(user)):
            raise ToolAccessError("Аналитика расписания недоступна для вашей роли.")
        chart = _schedule_bar_chart(db, arguments)
    elif subject == "payroll":
        chart = _payroll_bar_chart(db, arguments, user)
    elif subject == "clients":
        chart = _clients_bar_chart(db, arguments, user)
    elif subject == "fleet":
        chart = _fleet_bar_chart(db, arguments, user, boats)
    else:
        raise ValueError("Неизвестный раздел данных для графика.")
    return {
        "visualization": chart,
        "note": "Значения рассчитаны сервером по данным системы.",
    }


TOOL_SCHEMAS = {
    "get_data_catalog": {
        "description": (
            "Получить каталог доступных текущему пользователю наборов бизнес-данных: "
            "их показатели, группировки, фильтры, даты и ограничения. Используй перед "
            "аналитикой, если не уверен, какой источник или инструмент подходит."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "enum": list(DATASET_IDS)},
            },
            "additionalProperties": False,
        },
    },
    "get_system_guide": {
        "description": "Получить справку о работе конкретного раздела Бодрого Бизнеса.",
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "enum": list(GUIDE_TOPICS)},
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
    },
    "get_schedule_summary": {
        "description": "Получить агрегированную сводку внутреннего расписания без персональных данных клиентов.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "boat": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "get_fleet_status": {
        "description": "Получить остатки топлива и количество текущих неисправностей по флоту.",
        "parameters": {
            "type": "object",
            "properties": {"boat": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    "get_tuning_summary": {
        "description": (
            "Получить агрегированную сводку тюнинг-заказов по бизнес-дате из интерфейса: "
            "количество и стоимость заказов, оплаты, текущую задолженность, статусы и каналы продаж."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "status": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "get_clients_summary": {
        "description": "Получить количество клиентов и агрегаты по статусам и каналам без имён и телефонов.",
        "parameters": {
            "type": "object",
            "properties": {
                "segment": {"type": "string", "enum": ["excursion", "tuning"]},
            },
            "required": ["segment"],
            "additionalProperties": False,
        },
    },
    "get_payroll_summary": {
        "description": "Получить сводку начислений. Сотруднику доступны только собственные начисления.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "employee_name": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "get_tasks_summary": {
        "description": "Получить количество ожидающих и принятых задач по неисправностям и тюнингу.",
        "parameters": {
            "type": "object",
            "properties": {"employee_name": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    "get_employees_directory": {
        "description": (
            "Получить доступный только администратору справочник сотрудников: имена, "
            "должности, активность и наличие личного кабинета или привязки Telegram. "
            "Логины, пароли, хеши и Telegram ID никогда не возвращаются."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "position": {"type": "string", "maxLength": 80},
                "activity": {
                    "type": "string",
                    "enum": ["active", "deleted", "all"],
                },
                "account_state": {
                    "type": "string",
                    "enum": ["all", "created", "missing"],
                },
                "telegram_state": {
                    "type": "string",
                    "enum": ["all", "linked", "missing"],
                },
                "include_names": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    "get_business_overview": {
        "description": "Получить общий агрегированный обзор расписания, тюнинга, зарплат и флота.",
        "parameters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "additionalProperties": False,
        },
    },
    "get_bar_chart": {
        "description": (
            "Построить безопасную столбчатую диаграмму по данным системы. "
            "Используй эту функцию, когда пользователь просит показать график или диаграмму. "
            "Допустимые сочетания: tuning — orders/amount_rub по day/month/status/"
            "sale_channel/equipment_type; schedule — trips/revenue_rub/guests по "
            "day/month/boat/service/kind; payroll — entries/amount_rub по day/month/"
            "employee/work_type; clients — clients по status/acquisition_channel; "
            "fleet — tank_liters/reserve_liters/defects (group_by не нужен)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {
                    "type": "string",
                    "enum": ["tuning", "schedule", "payroll", "clients", "fleet"],
                },
                "metric": {
                    "type": "string",
                    "enum": [
                        "orders", "amount_rub", "trips", "revenue_rub", "guests",
                        "entries", "clients", "tank_liters", "reserve_liters", "defects",
                    ],
                },
                "group_by": {
                    "type": "string",
                    "enum": [
                        "day", "month", "status", "sale_channel", "equipment_type",
                        "boat", "service", "kind", "employee", "work_type",
                        "acquisition_channel",
                    ],
                },
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "employee_name": {"type": "string"},
                "segment": {"type": "string", "enum": ["excursion", "tuning"]},
            },
            "required": ["subject", "metric"],
            "additionalProperties": False,
        },
    },
}


def allowed_tool_names(user):
    names = [
        "get_data_catalog", "get_system_guide", "get_payroll_summary",
        "get_tasks_summary", "get_bar_chart",
    ]
    if _is_admin(user):
        names.extend([
            "get_schedule_summary",
            "get_fleet_status",
            "get_tuning_summary",
            "get_clients_summary",
            "get_employees_directory",
            "get_business_overview",
        ])
    elif _is_manager(user):
        names.extend(["get_schedule_summary", "get_clients_summary"])
    elif _is_captain(user):
        names.append("get_fleet_status")
    return names


def tool_definitions(user):
    definitions = []
    for name in allowed_tool_names(user):
        parameters = TOOL_SCHEMAS[name]["parameters"]
        if name == "get_data_catalog":
            parameters = {
                "type": "object",
                "properties": {
                    "dataset": {
                        "type": "string",
                        "enum": list(visible_dataset_ids(user)),
                    },
                },
                "additionalProperties": False,
            }
        elif name == "get_bar_chart":
            parameters = {
                **parameters,
                "properties": {
                    **parameters["properties"],
                    "subject": {
                        **parameters["properties"]["subject"],
                        "enum": list(visible_chart_subjects(user)),
                    },
                },
            }
        definitions.append({
            "type": "function",
            "name": name,
            "description": TOOL_SCHEMAS[name]["description"],
            "parameters": parameters,
        })
    return definitions


def execute_tool(db, user, boats, name, arguments):
    if name not in allowed_tool_names(user):
        raise ToolAccessError("Этот источник данных недоступен для вашей роли.")
    arguments = arguments if isinstance(arguments, dict) else {}
    if name == "get_data_catalog":
        return catalog_for_user(user, str(arguments.get("dataset") or "").strip() or None)
    if name == "get_system_guide":
        topic = str(arguments.get("topic") or "overview")
        if topic not in GUIDE_TOPICS:
            raise ValueError("Неизвестный раздел справки.")
        return {"topic": GUIDE_TOPICS[topic], "guide": guide_for(topic)}
    if name == "get_schedule_summary":
        return _schedule_summary(db, arguments)
    if name == "get_fleet_status":
        return _fleet_status(db, arguments, boats)
    if name == "get_tuning_summary":
        return _tuning_summary(db, arguments)
    if name == "get_clients_summary":
        return _clients_summary(db, arguments, user)
    if name == "get_payroll_summary":
        return _payroll_summary(db, arguments, user)
    if name == "get_tasks_summary":
        return _tasks_summary(db, arguments, user)
    if name == "get_employees_directory":
        return _employees_directory(db, arguments, user)
    if name == "get_business_overview":
        return _business_overview(db, arguments, user, boats)
    if name == "get_bar_chart":
        return _bar_chart(db, arguments, user, boats)
    raise ValueError("Неизвестный инструмент.")

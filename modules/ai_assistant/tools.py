"""Role-aware, read-only tools available to the language model."""

import datetime as dt

from .constants import GUIDE_TOPICS
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


TOOL_SCHEMAS = {
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
}


def allowed_tool_names(user):
    names = ["get_system_guide", "get_payroll_summary", "get_tasks_summary"]
    if _is_admin(user):
        names.extend([
            "get_schedule_summary",
            "get_fleet_status",
            "get_tuning_summary",
            "get_clients_summary",
            "get_business_overview",
        ])
    elif _is_manager(user):
        names.extend(["get_schedule_summary", "get_clients_summary"])
    elif _is_captain(user):
        names.append("get_fleet_status")
    return names


def tool_definitions(user):
    return [
        {
            "type": "function",
            "name": name,
            "description": TOOL_SCHEMAS[name]["description"],
            "parameters": TOOL_SCHEMAS[name]["parameters"],
        }
        for name in allowed_tool_names(user)
    ]


def execute_tool(db, user, boats, name, arguments):
    if name not in allowed_tool_names(user):
        raise ToolAccessError("Этот источник данных недоступен для вашей роли.")
    arguments = arguments if isinstance(arguments, dict) else {}
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
    if name == "get_business_overview":
        return _business_overview(db, arguments, user, boats)
    raise ValueError("Неизвестный инструмент.")

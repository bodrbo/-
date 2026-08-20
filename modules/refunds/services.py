"""Safe reconciliation and refund rules for excursion payments."""

import datetime as dt
import json
import re
import secrets
import sqlite3
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

import requests

from . import repository


REFUND_STATUS_LABELS = {
    "submitting": "Отправляется",
    "pending": "Обрабатывается",
    "unknown": "Нужно проверить",
    "succeeded": "Возвращён",
    "canceled": "Отклонён",
    "failed": "Не создан",
}
PAYMENT_METHOD_LABELS = {
    "bank_card": "Банковская карта",
    "sberbank": "SberPay",
    "sbp": "СБП",
    "yoo_money": "ЮMoney",
    "tinkoff_bank": "T-Pay",
}
EXPLICIT_RECORD_METADATA_KEYS = (
    "yclients_record_id",
    "record_id",
    "yclients_booking_id",
    "booking_id",
)


def current_timestamp():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _money_cents(value):
    try:
        decimal = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal.is_finite():
        return None
    return int(decimal * 100)


def _money_value(cents):
    return float(Decimal(cents) / Decimal(100))


def _api_money(cents):
    return f"{Decimal(cents) / Decimal(100):.2f}"


def _local_iso_datetime(raw_value):
    raw = str(raw_value or "").strip()
    if not raw:
        return ""
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:19]
    if value.tzinfo is not None:
        value = value.astimezone(ZoneInfo("Europe/Moscow"))
    return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M")


def _record_client(record):
    client = record.get("client") or {}
    if not client and record.get("clients"):
        client = (record.get("clients") or [{}])[0] or {}
    return client if isinstance(client, dict) else {}


def _record_expected_amount(record):
    total = Decimal("0")
    for service in record.get("services") or []:
        raw_cost = service.get("cost_to_pay")
        if raw_cost is None:
            raw_cost = service.get("cost") or 0
            raw_cost = Decimal(str(raw_cost)) * Decimal(str(service.get("amount") or 1))
        try:
            total += Decimal(str(raw_cost))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _record_snapshot(record, synced_at):
    record_id = record.get("id")
    try:
        record_id = int(record_id)
    except (TypeError, ValueError):
        return None
    client = _record_client(record)
    services = [
        str(service.get("title") or "").strip()
        for service in (record.get("services") or [])
        if str(service.get("title") or "").strip()
    ]
    if not client or not any(
        client.get(field) for field in ("id", "name", "phone", "email")
    ):
        return None
    trip_at = _local_iso_datetime(record.get("datetime") or record.get("date"))
    if not trip_at:
        return None
    return {
        "yclients_record_id": record_id,
        "activity_id": record.get("activity_id") or None,
        "visit_id": record.get("visit_id") or None,
        "trip_at": trip_at,
        "service_title": ", ".join(services)[:500] or "Экскурсионный рейс",
        "client_name": str(client.get("name") or "").strip()[:200],
        "client_phone": str(client.get("phone") or "").strip()[:80],
        "client_email": str(client.get("email") or "").strip()[:200],
        "expected_amount": _record_expected_amount(record),
        "paid_full": 1 if record.get("paid_full") else 0,
        "prepaid": 1 if record.get("prepaid") else 0,
        "prepaid_confirmed": 1 if record.get("prepaid_confirmed") else 0,
        "is_online": 1 if record.get("online") else 0,
        "is_deleted": 1 if record.get("deleted") else 0,
        "raw_json": json.dumps(record, ensure_ascii=False, separators=(",", ":")),
        "last_synced_at": synced_at,
    }


def sync_yclients_records(db, records):
    synced_at = current_timestamp()
    saved = 0
    for raw_record in records:
        snapshot = _record_snapshot(raw_record, synced_at)
        if snapshot is None:
            continue
        repository.upsert_record(db, snapshot)
        saved += 1
    repository.commit(db)
    return saved


def _remote_payment_snapshot(remote):
    amount = (remote.get("amount") or {}).get("value")
    refunded = (remote.get("refunded_amount") or {}).get("value") or 0
    method = remote.get("payment_method") or {}
    card = method.get("card") or {}
    method_type = str(method.get("type") or "").strip()
    return {
        "yookassa_payment_id": str(remote.get("id") or "").strip(),
        "amount": _money_value(_money_cents(amount) or 0),
        "currency": str((remote.get("amount") or {}).get("currency") or "RUB"),
        "refunded_amount": _money_value(_money_cents(refunded) or 0),
        "status": str(remote.get("status") or "unknown"),
        "refundable": 1 if remote.get("refundable") else 0,
        "description": str(remote.get("description") or "").strip()[:500],
        "payment_method": PAYMENT_METHOD_LABELS.get(method_type, method.get("title") or method_type),
        "card_last4": str(card.get("last4") or "").strip()[:8],
        "metadata_json": json.dumps(remote.get("metadata") or {}, ensure_ascii=False),
        "remote_created_at": _local_iso_datetime(remote.get("created_at")),
        "last_synced_at": current_timestamp(),
    }


def _metadata_record_id(metadata):
    if not isinstance(metadata, dict):
        return None
    for key in EXPLICIT_RECORD_METADATA_KEYS:
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return int(str(value).strip())
        except ValueError:
            continue
    return None


def sync_remote_payment(db, remote, linked_by="Система"):
    payment_id = str(remote.get("id") or "").strip()
    metadata = remote.get("metadata") or {}
    if not payment_id or metadata.get("tuning_order_id") or repository.is_tuning_payment(db, payment_id):
        return None, False
    snapshot = _remote_payment_snapshot(remote)
    payment = repository.upsert_payment(db, snapshot)
    auto_linked = False
    if payment["yclients_record_id"] is None:
        record_id = _metadata_record_id(metadata)
        if record_id is not None and repository.get_record(db, record_id) is not None:
            repository.link_payment(
                db, payment["id"], record_id, "metadata", linked_by, current_timestamp()
            )
            auto_linked = True
    repository.commit(db)
    return repository.get_payment_by_remote_id(db, payment_id), auto_linked


def sync_yookassa_payments(db, api_request, since_date, max_pages=20):
    cursor = None
    scanned = saved = auto_linked = 0
    reached_older = False
    for _ in range(max_pages):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        payload = api_request("GET", "/payments", params=params)
        items = payload.get("items") or []
        if not items:
            break
        for remote in items:
            scanned += 1
            created_raw = str(remote.get("created_at") or "")[:10]
            if created_raw and created_raw < since_date:
                reached_older = True
                continue
            if remote.get("status") != "succeeded":
                continue
            payment, was_linked = sync_remote_payment(db, remote)
            if payment is not None:
                saved += 1
                auto_linked += int(was_linked)
        cursor = payload.get("next_cursor")
        if reached_older or not cursor:
            break
    refunds_saved = sync_yookassa_refunds(db, api_request, since_date, max_pages)
    sync_open_refunds(db, api_request)
    return {
        "scanned": scanned,
        "saved": saved,
        "auto_linked": auto_linked,
        "refunds_saved": refunds_saved,
    }


def sync_yookassa_refunds(db, api_request, since_date, max_pages=20):
    cursor = None
    saved = 0
    reached_older = False
    for _ in range(max_pages):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        payload = api_request("GET", "/refunds", params=params)
        items = payload.get("items") or []
        if not items:
            break
        for remote in items:
            created_raw = str(remote.get("created_at") or "")[:10]
            if created_raw and created_raw < since_date:
                reached_older = True
                continue
            saved += int(apply_remote_refund(db, remote))
        cursor = payload.get("next_cursor")
        if reached_older or not cursor:
            break
    return saved


def sync_open_refunds(db, api_request):
    synced = 0
    for refund in repository.list_open_refunds(db):
        if not refund["yookassa_refund_id"]:
            continue
        remote = api_request("GET", f"/refunds/{refund['yookassa_refund_id']}")
        apply_remote_refund(db, remote)
        synced += 1
    return synced


def link_remote_payment(db, record_id, remote_payment_id, api_request, actor_name):
    record = repository.get_record(db, record_id)
    if record is None:
        return False, "Запись YCLIENTS не найдена. Сначала обновите список рейсов."
    remote_payment_id = str(remote_payment_id or "").strip()
    if not remote_payment_id:
        return False, "Введите идентификатор платежа ЮKassa."
    remote = api_request("GET", f"/payments/{remote_payment_id}")
    if remote.get("status") != "succeeded":
        return False, "Возврат доступен только для платежа со статусом «Успешно»."
    if (remote.get("amount") or {}).get("currency") != "RUB":
        return False, "Поддерживаются только платежи в рублях."
    payment, _ = sync_remote_payment(db, remote)
    if payment is None:
        return False, "Этот платёж относится к заказу тюнинг-центра и не может быть связан с рейсом."
    if payment["yclients_record_id"] not in (None, record_id):
        return False, "Этот платёж уже связан с другой записью YCLIENTS."
    repository.link_payment(
        db, payment["id"], record_id, "manual", actor_name, current_timestamp()
    )
    repository.commit(db)
    return True, f"Платёж {remote_payment_id} связан с записью YCLIENTS №{record_id}."


def link_stored_payment(db, payment_id, record_id, actor_name):
    payment = repository.get_payment(db, payment_id)
    record = repository.get_record(db, record_id)
    if payment is None or record is None:
        return False, "Платёж или запись YCLIENTS не найдены."
    if payment["yclients_record_id"] not in (None, record_id):
        return False, "Этот платёж уже связан с другой записью."
    if repository.list_refunds(db, payment_id):
        return False, "Платёж с историей возвратов нельзя перепривязать."
    repository.link_payment(
        db, payment_id, record_id, "manual", actor_name, current_timestamp()
    )
    repository.commit(db)
    return True, "Платёж связан с выбранной записью YCLIENTS."


def unlink_payment(db, payment_id):
    payment = repository.get_payment(db, payment_id)
    if payment is None:
        return False, "Платёж не найден."
    if repository.list_refunds(db, payment_id):
        return False, "Связь нельзя удалить после создания возврата."
    repository.unlink_payment(db, payment_id)
    repository.commit(db)
    return True, "Связь платежа с записью удалена."


def _refund_view(db, refund):
    item = dict(refund)
    item["status_label"] = REFUND_STATUS_LABELS.get(item["status"], item["status"])
    return item


def _payment_view(db, payment):
    item = dict(payment)
    refunds = [_refund_view(db, row) for row in repository.list_refunds(db, item["id"])]
    local_observed_cents = max(
        (
            (_money_cents(refund["refunded_before"]) or 0)
            + (_money_cents(refund["amount"]) or 0)
            for refund in refunds
            if refund["status"] == "succeeded"
        ),
        default=0,
    )
    remote_refunded_cents = _money_cents(item["refunded_amount"]) or 0
    refunded_cents = max(local_observed_cents, remote_refunded_cents)
    amount_cents = _money_cents(item["amount"]) or 0
    available_cents = max(0, amount_cents - refunded_cents)
    item.update(
        refunds=refunds,
        refunded_amount_effective=_money_value(refunded_cents),
        available_amount=_money_value(available_cents),
        returned_percent=(round(refunded_cents / amount_cents * 100, 1) if amount_cents else 0),
        can_refund=(
            item["status"] == "succeeded"
            and bool(item["refundable"])
            and available_cents > 0
            and not repository.has_open_refund(db, item["id"])
        ),
        has_open_refund=repository.has_open_refund(db, item["id"]),
        operation_key=secrets.token_hex(16),
    )
    return item


def dashboard(db, start_date, end_date, search="", status_filter="all"):
    records = [dict(row) for row in repository.list_records(db, start_date, end_date, search)]
    payments_by_record = {}
    unmatched = []
    for row in repository.list_payments(db):
        payment = _payment_view(db, row)
        if payment["yclients_record_id"] is None:
            unmatched.append(payment)
        else:
            payments_by_record.setdefault(payment["yclients_record_id"], []).append(payment)

    result = []
    for record in records:
        record["payments"] = payments_by_record.get(record["yclients_record_id"], [])
        record["returned_amount"] = sum(
            payment["refunded_amount_effective"] for payment in record["payments"]
        )
        record["available_amount"] = sum(
            payment["available_amount"] for payment in record["payments"]
        )
        if status_filter == "linked" and not record["payments"]:
            continue
        if status_filter == "unlinked" and record["payments"]:
            continue
        if status_filter == "refunded" and record["returned_amount"] <= 0:
            continue
        result.append(record)

    all_refunds = repository.list_refunds(db)
    return {
        "records": result,
        "unmatched_payments": unmatched,
        "record_options": records,
        "linked_count": sum(1 for record in records if payments_by_record.get(record["yclients_record_id"])),
        "unmatched_count": len(unmatched),
        "successful_refunds": sum(1 for refund in all_refunds if refund["status"] == "succeeded"),
        "successful_refund_amount": sum(
            refund["amount"] for refund in all_refunds if refund["status"] == "succeeded"
        ),
    }


def _valid_email(value):
    value = str(value or "").strip()
    return value if len(value) <= 200 and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) else None


def _refund_body(record, payment, amount_cents, kind, reason, email, vat_code, payment_mode):
    body = {
        "payment_id": payment["yookassa_payment_id"],
        "amount": {"value": _api_money(amount_cents), "currency": "RUB"},
        "description": f"Возврат по записи YCLIENTS №{record['yclients_record_id']}: {reason}"[:128],
    }
    if kind == "partial":
        body["receipt"] = {
            "customer": {"email": email},
            "items": [
                {
                    "description": f"{record['service_title']} · {record['trip_at'][:10]}"[:128],
                    "quantity": 1,
                    "amount": {"value": _api_money(amount_cents), "currency": "RUB"},
                    "vat_code": vat_code,
                    "measure": "piece",
                    "payment_subject": "service",
                    "payment_mode": payment_mode,
                }
            ],
        }
    return body


def create_refund(
    db,
    payment_id,
    raw_amount,
    mode,
    reason,
    raw_email,
    operation_key,
    confirmed,
    actor_name,
    api_request,
    vat_code,
    payment_mode,
):
    payment = repository.get_payment(db, payment_id)
    if payment is None or payment["yclients_record_id"] is None:
        return False, "Сначала свяжите платёж с записью YCLIENTS."
    record = repository.get_record(db, payment["yclients_record_id"])
    if record is None:
        return False, "Связанная запись YCLIENTS не найдена."
    if not confirmed:
        return False, "Подтвердите, что возврат отправляет реальные деньги клиенту."
    reason = " ".join(str(reason or "").strip().split())
    if len(reason) < 3 or len(reason) > 300:
        return False, "Укажите причину возврата длиной от 3 до 300 символов."
    if not re.fullmatch(r"[0-9a-f]{32}", str(operation_key or "")):
        return False, "Страница устарела. Обновите её и повторите возврат."
    existing = repository.get_refund_by_idempotence_key(db, operation_key)
    if existing is not None:
        return False, "Этот возврат уже был отправлен. Обновите страницу."
    if repository.has_open_refund(db, payment_id):
        return False, "По платежу уже обрабатывается возврат. Сначала проверьте его статус."

    remote = api_request("GET", f"/payments/{payment['yookassa_payment_id']}")
    payment, _ = sync_remote_payment(db, remote)
    view = _payment_view(db, payment)
    available_cents = _money_cents(view["available_amount"]) or 0
    if (
        payment["status"] != "succeeded"
        or not payment["refundable"]
        or available_cents <= 0
    ):
        return False, "У платежа нет суммы, доступной для возврата."

    if mode == "full":
        amount_cents = available_cents
    else:
        amount_cents = _money_cents(str(raw_amount or "").strip().replace(",", "."))
        if amount_cents is None or amount_cents < 100:
            return False, "Частичный возврат должен быть не меньше 1 ₽."
        if amount_cents > available_cents:
            return False, "Сумма возврата превышает доступный остаток платежа."
    remaining_cents = available_cents - amount_cents
    original_cents = _money_cents(payment["amount"]) or 0
    refunded_before_cents = max(0, original_cents - available_cents)
    kind = (
        "full"
        if refunded_before_cents == 0 and amount_cents == original_cents
        else "partial"
    )
    if remaining_cents != 0 and remaining_cents < 100:
        return False, "После частичного возврата должно остаться не меньше 1 ₽ либо 0 ₽."

    email = None
    if kind == "partial":
        email = _valid_email(raw_email) or _valid_email(record["client_email"])
        if email is None:
            return False, "Для чека частичного возврата укажите email клиента."

    body = _refund_body(
        record, payment, amount_cents, kind, reason, email, vat_code, payment_mode
    )
    body["metadata"] = {
        "excursion_refund_key": operation_key,
        "yclients_record_id": str(record["yclients_record_id"]),
    }
    timestamp = current_timestamp()
    refund_data = {
        "payment_id": payment["id"],
        "amount": _money_value(amount_cents),
        "status": "submitting",
        "refund_kind": kind,
        "reason": reason,
        "receipt_email": email,
        "refunded_before": _money_value(refunded_before_cents),
        "idempotence_key": operation_key,
        "request_json": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        "created_by": actor_name,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    try:
        refund = repository.insert_refund(db, refund_data)
        repository.commit(db)
    except sqlite3.IntegrityError:
        return False, "Этот возврат уже отправляется. Обновите страницу."

    try:
        remote_refund = api_request(
            "POST", "/refunds", json_body=body, idempotence_key=operation_key
        )
    except requests.RequestException as error:
        repository.update_refund_remote(
            db, refund["id"], None, "unknown", None, None, str(error)[:500], current_timestamp()
        )
        repository.commit(db)
        return False, "ЮKassa не подтвердила ответ. Не создавайте новый возврат — нажмите «Повторить проверку»."
    except Exception as error:
        repository.update_refund_remote(
            db, refund["id"], None, "failed", None, None, str(error)[:500], current_timestamp()
        )
        repository.commit(db)
        return False, f"ЮKassa отклонила запрос: {error}"

    apply_remote_refund(db, remote_refund, refund["id"])
    status = remote_refund.get("status")
    if status == "succeeded":
        try:
            fresh_payment = api_request("GET", f"/payments/{payment['yookassa_payment_id']}")
            sync_remote_payment(db, fresh_payment)
        except Exception:
            pass
        return True, f"Клиенту возвращено {_api_money(amount_cents)} ₽."
    if status == "pending":
        return True, "Возврат принят ЮKassa и находится в обработке."
    return False, "ЮKassa не выполнила возврат. Причина сохранена в журнале."


def retry_unknown_refund(db, refund_id, api_request):
    refund = repository.get_refund(db, refund_id)
    if refund is None or refund["status"] != "unknown":
        return False, "Повторная проверка для этого возврата недоступна."
    payment = repository.get_payment(db, refund["payment_id"])
    if payment is None:
        return False, "Связанный платёж не найден."
    try:
        remote_payment = api_request(
            "GET", f"/payments/{payment['yookassa_payment_id']}"
        )
        remote_refunded = _money_cents(
            (remote_payment.get("refunded_amount") or {}).get("value") or 0
        ) or 0
        expected_refunded = (_money_cents(refund["refunded_before"]) or 0) + (
            _money_cents(refund["amount"]) or 0
        )
        sync_remote_payment(db, remote_payment)
        if remote_refunded >= expected_refunded:
            repository.update_refund_remote(
                db,
                refund["id"],
                None,
                "succeeded",
                None,
                None,
                "Результат подтверждён по увеличившейся возвращённой сумме платежа.",
                current_timestamp(),
            )
            repository.commit(db)
            return True, "Возврат подтверждён по актуальному остатку платежа ЮKassa."
    except Exception:
        return False, "Не удалось проверить актуальный остаток платежа. Попробуйте позже."

    try:
        created_at = dt.datetime.fromisoformat(refund["created_at"])
        age = dt.datetime.fromisoformat(current_timestamp()) - created_at
    except (TypeError, ValueError):
        age = dt.timedelta(days=2)
    if age > dt.timedelta(hours=23):
        return False, (
            "Прошло больше 23 часов. Автоматический повтор заблокирован: "
            "сначала проверьте возврат в кабинете ЮKassa."
        )

    body = json.loads(refund["request_json"])
    try:
        remote = api_request(
            "POST",
            "/refunds",
            json_body=body,
            idempotence_key=refund["idempotence_key"],
        )
    except Exception as error:
        repository.update_refund_remote(
            db, refund["id"], None, "unknown", None, None, str(error)[:500], current_timestamp()
        )
        repository.commit(db)
        return False, "ЮKassa всё ещё не подтвердила результат. Новый возврат не создавался."
    apply_remote_refund(db, remote, refund["id"])
    return True, "Статус возврата получен из ЮKassa."


def apply_remote_refund(db, remote, local_refund_id=None):
    remote_id = str(remote.get("id") or "").strip()
    if not remote_id:
        return False
    refund = repository.get_refund(db, local_refund_id) if local_refund_id else None
    if refund is None:
        refund = repository.get_refund_by_remote_id(db, remote_id)
    if refund is None:
        metadata = remote.get("metadata") or {}
        key = metadata.get("excursion_refund_key") or metadata.get("idempotence_key")
        refund = repository.get_refund_by_idempotence_key(db, key) if key else None
    if refund is None:
        payment_remote_id = str(remote.get("payment_id") or "")
        payment = repository.get_payment_by_remote_id(db, payment_remote_id)
        if payment is None:
            return False
        amount_cents = _money_cents((remote.get("amount") or {}).get("value"))
        if amount_cents is None:
            return False
        timestamp = current_timestamp()
        refund = repository.insert_refund(
            db,
            {
                "payment_id": payment["id"],
                "amount": _money_value(amount_cents),
                "status": str(remote.get("status") or "pending"),
                "refund_kind": "external",
                "reason": "Создано вне системы",
                "receipt_email": None,
                "refunded_before": 0,
                "idempotence_key": f"external-{remote_id}",
                "request_json": "{}",
                "created_by": "ЮKassa",
                "created_at": timestamp,
                "updated_at": timestamp,
            },
        )
    cancellation = remote.get("cancellation_details") or {}
    cancellation_reason = ": ".join(
        value for value in (cancellation.get("party"), cancellation.get("reason")) if value
    )
    repository.update_refund_remote(
        db,
        refund["id"],
        remote_id,
        str(remote.get("status") or refund["status"]),
        remote.get("receipt_registration"),
        cancellation_reason or None,
        None,
        current_timestamp(),
    )
    repository.commit(db)
    return True

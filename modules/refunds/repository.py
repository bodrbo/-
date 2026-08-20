"""SQL access for YCLIENTS excursion bookings, YooKassa payments and refunds."""


def upsert_record(db, record):
    existing = get_record(db, record["yclients_record_id"])
    values = (
        record["activity_id"],
        record["visit_id"],
        record["trip_at"],
        record["service_title"],
        record["client_name"],
        record["client_phone"],
        record["client_email"],
        record["expected_amount"],
        record["paid_full"],
        record["prepaid"],
        record["prepaid_confirmed"],
        record["is_online"],
        record["is_deleted"],
        record["raw_json"],
        record["last_synced_at"],
        record["yclients_record_id"],
    )
    if existing is None:
        db.execute(
            "INSERT INTO excursion_refund_records "
            "(activity_id, visit_id, trip_at, service_title, client_name, client_phone, "
            "client_email, expected_amount, paid_full, prepaid, prepaid_confirmed, "
            "is_online, is_deleted, raw_json, last_synced_at, yclients_record_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    else:
        db.execute(
            "UPDATE excursion_refund_records SET activity_id = ?, visit_id = ?, "
            "trip_at = ?, service_title = ?, client_name = ?, client_phone = ?, "
            "client_email = ?, expected_amount = ?, paid_full = ?, prepaid = ?, "
            "prepaid_confirmed = ?, is_online = ?, is_deleted = ?, raw_json = ?, "
            "last_synced_at = ? WHERE yclients_record_id = ?",
            values,
        )


def get_record(db, yclients_record_id):
    return db.execute(
        "SELECT * FROM excursion_refund_records WHERE yclients_record_id = ?",
        (yclients_record_id,),
    ).fetchone()


def list_records(db, start_date, end_date, search=""):
    query = (
        "SELECT * FROM excursion_refund_records "
        "WHERE substr(trip_at, 1, 10) BETWEEN ? AND ?"
    )
    params = [start_date, end_date]
    if search:
        query += (
            " AND (CAST(yclients_record_id AS TEXT) LIKE ? OR client_name LIKE ? "
            "OR client_phone LIKE ? OR service_title LIKE ?)"
        )
        needle = f"%{search}%"
        params.extend([needle, needle, needle, needle])
    query += " ORDER BY trip_at DESC, yclients_record_id DESC"
    return db.execute(query, params).fetchall()


def is_tuning_payment(db, yookassa_payment_id):
    return db.execute(
        "SELECT 1 FROM tuning_yookassa_payments WHERE yookassa_payment_id = ?",
        (yookassa_payment_id,),
    ).fetchone() is not None


def upsert_payment(db, payment):
    existing = get_payment_by_remote_id(db, payment["yookassa_payment_id"])
    values = (
        payment["amount"],
        payment["currency"],
        payment["refunded_amount"],
        payment["status"],
        payment["refundable"],
        payment["description"],
        payment["payment_method"],
        payment["card_last4"],
        payment["metadata_json"],
        payment["remote_created_at"],
        payment["last_synced_at"],
        payment["yookassa_payment_id"],
    )
    if existing is None:
        cursor = db.execute(
            "INSERT INTO excursion_yookassa_payments "
            "(amount, currency, refunded_amount, status, refundable, description, "
            "payment_method, card_last4, metadata_json, remote_created_at, last_synced_at, "
            "yookassa_payment_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        return get_payment(db, cursor.lastrowid)
    db.execute(
        "UPDATE excursion_yookassa_payments SET amount = ?, currency = ?, "
        "refunded_amount = ?, status = ?, refundable = ?, description = ?, "
        "payment_method = ?, card_last4 = ?, metadata_json = ?, remote_created_at = ?, "
        "last_synced_at = ? WHERE yookassa_payment_id = ?",
        values,
    )
    return get_payment_by_remote_id(db, payment["yookassa_payment_id"])


def get_payment(db, payment_id):
    return db.execute(
        "SELECT * FROM excursion_yookassa_payments WHERE id = ?", (payment_id,)
    ).fetchone()


def get_payment_by_remote_id(db, yookassa_payment_id):
    return db.execute(
        "SELECT * FROM excursion_yookassa_payments WHERE yookassa_payment_id = ?",
        (yookassa_payment_id,),
    ).fetchone()


def list_payments(db):
    return db.execute(
        "SELECT * FROM excursion_yookassa_payments "
        "ORDER BY remote_created_at DESC, id DESC"
    ).fetchall()


def list_open_refunds(db):
    return db.execute(
        "SELECT * FROM excursion_refunds WHERE status IN ('submitting', 'pending', 'unknown') "
        "ORDER BY id"
    ).fetchall()


def link_payment(db, payment_id, yclients_record_id, link_method, linked_by, linked_at):
    db.execute(
        "UPDATE excursion_yookassa_payments SET yclients_record_id = ?, link_method = ?, "
        "linked_by = ?, linked_at = ? WHERE id = ?",
        (yclients_record_id, link_method, linked_by, linked_at, payment_id),
    )


def unlink_payment(db, payment_id):
    db.execute(
        "UPDATE excursion_yookassa_payments SET yclients_record_id = NULL, "
        "link_method = NULL, linked_by = NULL, linked_at = NULL WHERE id = ?",
        (payment_id,),
    )


def list_refunds(db, payment_id=None):
    if payment_id is None:
        return db.execute(
            "SELECT * FROM excursion_refunds ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return db.execute(
        "SELECT * FROM excursion_refunds WHERE payment_id = ? "
        "ORDER BY created_at DESC, id DESC",
        (payment_id,),
    ).fetchall()


def get_refund(db, refund_id):
    return db.execute(
        "SELECT * FROM excursion_refunds WHERE id = ?", (refund_id,)
    ).fetchone()


def get_refund_by_remote_id(db, yookassa_refund_id):
    return db.execute(
        "SELECT * FROM excursion_refunds WHERE yookassa_refund_id = ?",
        (yookassa_refund_id,),
    ).fetchone()


def get_refund_by_idempotence_key(db, idempotence_key):
    return db.execute(
        "SELECT * FROM excursion_refunds WHERE idempotence_key = ?",
        (idempotence_key,),
    ).fetchone()


def has_open_refund(db, payment_id):
    return db.execute(
        "SELECT 1 FROM excursion_refunds WHERE payment_id = ? "
        "AND status IN ('submitting', 'pending', 'unknown') LIMIT 1",
        (payment_id,),
    ).fetchone() is not None


def insert_refund(db, refund):
    cursor = db.execute(
        "INSERT INTO excursion_refunds "
        "(payment_id, amount, status, refund_kind, reason, receipt_email, "
        "refunded_before, idempotence_key, request_json, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            refund["payment_id"],
            refund["amount"],
            refund["status"],
            refund["refund_kind"],
            refund["reason"],
            refund["receipt_email"],
            refund["refunded_before"],
            refund["idempotence_key"],
            refund["request_json"],
            refund["created_by"],
            refund["created_at"],
            refund["updated_at"],
        ),
    )
    return get_refund(db, cursor.lastrowid)


def update_refund_remote(
    db,
    refund_id,
    yookassa_refund_id,
    status,
    receipt_registration,
    cancellation_reason,
    error_message,
    updated_at,
):
    db.execute(
        "UPDATE excursion_refunds SET yookassa_refund_id = COALESCE(?, yookassa_refund_id), "
        "status = ?, receipt_registration = ?, cancellation_reason = ?, "
        "error_message = ?, updated_at = ? WHERE id = ?",
        (
            yookassa_refund_id,
            status,
            receipt_registration,
            cancellation_reason,
            error_message,
            updated_at,
            refund_id,
        ),
    )


def commit(db):
    db.commit()

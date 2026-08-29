"""SQL access for the vessel fuel ledger and YCLIENTS trip queue."""


def get_state(db, boat):
    return db.execute(
        "SELECT * FROM boat_fuel_state WHERE boat = ?", (boat,)
    ).fetchone()


def activate_state(db, boat, activated_at, actor_role, actor_name, updated_at):
    db.execute(
        "UPDATE boat_fuel_state SET activated_at = ?, activated_by_role = ?, "
        "activated_by_name = ?, updated_at = ? WHERE boat = ?",
        (activated_at, actor_role, actor_name, updated_at, boat),
    )


def set_last_synced_at(db, boat, synced_at):
    db.execute(
        "UPDATE boat_fuel_state SET last_synced_at = ?, updated_at = ? WHERE boat = ?",
        (synced_at, synced_at, boat),
    )


def balance_at(db, boat, occurred_at=None):
    query = (
        "SELECT COALESCE(SUM(liters_delta), 0) AS balance "
        "FROM boat_fuel_transactions WHERE boat = ? AND deleted_at IS NULL"
    )
    params = [boat]
    if occurred_at is not None:
        query += " AND occurred_at <= ?"
        params.append(occurred_at)
    return float(db.execute(query, params).fetchone()["balance"] or 0)


def reserve_balance_at(db, boat, occurred_at=None):
    query = (
        "SELECT COALESCE(SUM(reserve_delta), 0) AS balance "
        "FROM boat_fuel_transactions WHERE boat = ? AND deleted_at IS NULL"
    )
    params = [boat]
    if occurred_at is not None:
        query += " AND occurred_at <= ?"
        params.append(occurred_at)
    return float(db.execute(query, params).fetchone()["balance"] or 0)


def add_transaction(
    db,
    boat,
    kind,
    liters_delta,
    reported_liters,
    occurred_at,
    source_ref,
    source_label,
    actor_role,
    actor_name,
    created_at,
    reserve_delta=0,
):
    cursor = db.execute(
        "INSERT INTO boat_fuel_transactions "
        "(boat, kind, liters_delta, reserve_delta, reported_liters, occurred_at, source_ref, "
        "source_label, created_by_role, created_by_name, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            boat,
            kind,
            liters_delta,
            reserve_delta,
            reported_liters,
            occurred_at,
            source_ref,
            source_label,
            actor_role,
            actor_name,
            created_at,
        ),
    )
    return cursor.lastrowid


def get_transaction_by_source(db, source_ref):
    # Deleted imported rows deliberately remain discoverable by source_ref:
    # this tombstone prevents the hourly YCLIENTS sync from recreating a
    # canceled trip that an administrator explicitly removed from the ledger.
    return db.execute(
        "SELECT * FROM boat_fuel_transactions WHERE source_ref = ?", (source_ref,)
    ).fetchone()


def get_transaction(db, transaction_id, boat=None):
    if boat is None:
        return db.execute(
            "SELECT * FROM boat_fuel_transactions WHERE id = ?",
            (transaction_id,),
        ).fetchone()
    return db.execute(
        "SELECT * FROM boat_fuel_transactions WHERE id = ? AND boat = ?",
        (transaction_id, boat),
    ).fetchone()


def list_transactions(db, boat, limit=30):
    return db.execute(
        "SELECT * FROM boat_fuel_transactions WHERE boat = ? AND deleted_at IS NULL "
        "ORDER BY occurred_at DESC, id DESC LIMIT ?",
        (boat, limit),
    ).fetchall()


def count_active_transactions(db, boat):
    return db.execute(
        "SELECT COUNT(*) AS count FROM boat_fuel_transactions "
        "WHERE boat = ? AND deleted_at IS NULL AND ABS(liters_delta) > 0.0001",
        (boat,),
    ).fetchone()["count"]


def soft_delete_transaction(db, transaction_id, boat, deleted_at, deleted_by):
    cursor = db.execute(
        "UPDATE boat_fuel_transactions SET deleted_at = ?, deleted_by = ? "
        "WHERE id = ? AND boat = ? AND deleted_at IS NULL",
        (deleted_at, deleted_by, transaction_id, boat),
    )
    return cursor.rowcount > 0


def deactivate_state(db, boat, updated_at):
    db.execute(
        "UPDATE boat_fuel_state SET activated_at = NULL, activated_by_role = NULL, "
        "activated_by_name = NULL, last_synced_at = NULL, updated_at = ? WHERE boat = ?",
        (updated_at, boat),
    )


def delete_trip_events(db, boat):
    db.execute("DELETE FROM boat_fuel_trip_events WHERE boat = ?", (boat,))


def upsert_trip_event(
    db,
    source_ref,
    boat,
    trip_kind,
    started_at,
    ended_at,
    service_title,
    last_seen_at,
):
    existing = get_trip_event_by_source(db, source_ref)
    if existing is None:
        cursor = db.execute(
            "INSERT INTO boat_fuel_trip_events "
            "(source_ref, boat, trip_kind, started_at, ended_at, service_title, "
            "status, last_seen_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                source_ref,
                boat,
                trip_kind,
                started_at,
                ended_at,
                service_title,
                last_seen_at,
                last_seen_at,
            ),
        )
        return get_trip_event(db, cursor.lastrowid)

    db.execute(
        "UPDATE boat_fuel_trip_events SET boat = ?, trip_kind = ?, started_at = ?, "
        "ended_at = ?, service_title = ?, last_seen_at = ? WHERE source_ref = ?",
        (
            boat,
            trip_kind,
            started_at,
            ended_at,
            service_title,
            last_seen_at,
            source_ref,
        ),
    )
    return get_trip_event_by_source(db, source_ref)


def get_trip_event(db, event_id, boat=None):
    if boat is None:
        return db.execute(
            "SELECT * FROM boat_fuel_trip_events WHERE id = ?", (event_id,)
        ).fetchone()
    return db.execute(
        "SELECT * FROM boat_fuel_trip_events WHERE id = ? AND boat = ?",
        (event_id, boat),
    ).fetchone()


def get_trip_event_by_source(db, source_ref):
    return db.execute(
        "SELECT * FROM boat_fuel_trip_events WHERE source_ref = ?", (source_ref,)
    ).fetchone()


def delete_yclients_trip_by_source(db, source_ref):
    """Remove fuel rows for a trip that YCLIENTS now marks as cancelled.

    These rows are hard-deleted so reversing the cancellation can recreate
    the event and its debit on a later synchronization.
    """
    transaction = db.execute(
        "DELETE FROM boat_fuel_transactions WHERE source_ref = ?",
        (f"fuel-trip:{source_ref}",),
    )
    event = db.execute(
        "DELETE FROM boat_fuel_trip_events WHERE source_ref = ?",
        (source_ref,),
    )
    return transaction.rowcount > 0 or event.rowcount > 0


def mark_trip_consumed(db, event_id, liters, transaction_id):
    db.execute(
        "UPDATE boat_fuel_trip_events SET status = 'consumed', "
        "consumption_liters = ?, transaction_id = ? WHERE id = ?",
        (liters, transaction_id, event_id),
    )


def list_pending_trip_events(db, boat):
    return db.execute(
        "SELECT * FROM boat_fuel_trip_events WHERE boat = ? AND trip_kind = 'individual' "
        "AND status = 'pending' ORDER BY ended_at, id",
        (boat,),
    ).fetchall()

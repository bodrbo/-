"""Persistence helpers for idempotent offline operations."""


def init_schema(db):
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS offline_operations (
            client_operation_id TEXT PRIMARY KEY,
            employee_name TEXT NOT NULL,
            operation_type TEXT NOT NULL,
            server_record_id INTEGER NOT NULL,
            client_created_at TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_offline_operations_employee "
        "ON offline_operations (employee_name, received_at)"
    )


def get_operation(db, operation_id):
    return db.execute(
        "SELECT * FROM offline_operations WHERE client_operation_id = ?",
        (operation_id,),
    ).fetchone()


def record_operation(
    db,
    operation_id,
    employee_name,
    operation_type,
    server_record_id,
    client_created_at,
    received_at,
):
    db.execute(
        "INSERT INTO offline_operations "
        "(client_operation_id, employee_name, operation_type, server_record_id, "
        "client_created_at, received_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            employee_name,
            operation_type,
            server_record_id,
            client_created_at,
            received_at,
        ),
    )


def list_documents(db):
    return db.execute(
        "SELECT id, boat, title, filename, original_filename, uploaded_at "
        "FROM boat_documents ORDER BY boat, uploaded_at DESC, id DESC"
    ).fetchall()

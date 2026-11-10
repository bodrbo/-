"""SQL access for the client's equipment."""


def list_for_client(db, client_id):
    return db.execute(
        "SELECT * FROM client_equipment WHERE client_id = ? "
        "ORDER BY status = 'archive', id", (client_id,)
    ).fetchall()


def get(db, item_id):
    return db.execute("SELECT * FROM client_equipment WHERE id = ?", (item_id,)).fetchone()


def add(db, client_id, data, created_at):
    return db.execute(
        "INSERT INTO client_equipment (client_id, kind, name, serial_number, year, note, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (client_id, data["kind"], data["name"], data["serial_number"], data["year"], data["note"],
         data["status"], created_at),
    ).lastrowid


def update(db, item_id, data):
    db.execute(
        "UPDATE client_equipment SET kind = ?, name = ?, serial_number = ?, year = ?, note = ?, status = ? "
        "WHERE id = ?",
        (data["kind"], data["name"], data["serial_number"], data["year"], data["note"], data["status"], item_id),
    )


def delete(db, item_id):
    db.execute("DELETE FROM client_equipment WHERE id = ?", (item_id,))


def catalog_names(db):
    """Catalog model names per kind — suggestions for the name field."""
    result = {"boat": [], "motor": []}
    for row in db.execute(
        "SELECT equipment_type, model_name FROM tuning_boat_profiles ORDER BY model_name COLLATE NOCASE"
    ).fetchall():
        result.setdefault(row["equipment_type"], []).append(row["model_name"])
    return result

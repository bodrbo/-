"""Queries and stable values for the shared sales-channel directory."""

import secrets


def list_channels(db, include_tripster=True):
    channels = []
    for row in db.execute(
        "SELECT code, name, kind FROM sales_channels "
        "ORDER BY sort_order, name COLLATE NOCASE, id"
    ).fetchall():
        if not include_tripster and row["code"] == "tripster":
            continue
        channels.append({
            "value": row["code"],
            "label": row["name"],
            "kind": row["kind"],
        })

    partners = db.execute(
        "SELECT clients.id, clients.client_name, "
        "GROUP_CONCAT(DISTINCT client_segments.segment) AS segments "
        "FROM clients JOIN client_segments "
        "ON client_segments.client_id = clients.id "
        "WHERE client_segments.relationship_type = 'partner' "
        "GROUP BY clients.id, clients.client_name "
        "ORDER BY clients.client_name COLLATE NOCASE, clients.id"
    ).fetchall()
    for row in partners:
        segments = set((row["segments"] or "").split(","))
        if segments == {"tuning"}:
            suffix = "тюнинг"
        elif segments == {"excursion"}:
            suffix = "экскурсии"
        else:
            suffix = "тюнинг и экскурсии"
        channels.append({
            "value": f"partner:{row['id']}",
            "label": f"{row['client_name']} · партнёр ({suffix})",
            "kind": "partner",
            "partner_id": row["id"],
        })
    return channels


def channel_values(db, include_tripster=True):
    return {
        channel["value"]
        for channel in list_channels(db, include_tripster=include_tripster)
    }


def normalise_value(db, raw_value, include_tripster=True):
    value = str(raw_value or "").strip()
    # Compatibility with forms and rows created before the shared directory:
    # a bare integer used to mean the excursion partner client id.
    if value.isdigit():
        value = f"partner:{value}"
    return value if value in channel_values(db, include_tripster) else ""


def partner_id(value):
    prefix, separator, raw_id = str(value or "").partition(":")
    if prefix != "partner" or not separator or not raw_id.isdigit():
        return None
    return int(raw_id)


def label_map(db, include_tripster=True):
    return {
        channel["value"]: channel["label"]
        for channel in list_channels(db, include_tripster=include_tripster)
    }


def create_custom_channel(db, name, timestamp):
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        return None, "Укажите название канала продаж."
    if len(cleaned) > 120:
        return None, "Название канала — не более 120 символов."
    duplicate = db.execute(
        "SELECT code, name FROM sales_channels WHERE lower(name) = lower(?)",
        (cleaned,),
    ).fetchone()
    if duplicate is not None:
        return {
            "value": duplicate["code"], "label": duplicate["name"],
            "kind": "custom",
        }, None
    cursor = db.execute(
        "INSERT INTO sales_channels (code, name, kind, sort_order, created_at) "
        "VALUES (?, ?, 'custom', 80, ?)",
        (f"pending:{secrets.token_hex(8)}", cleaned, timestamp),
    )
    code = f"custom:{cursor.lastrowid}"
    db.execute(
        "UPDATE sales_channels SET code = ? WHERE id = ?",
        (code, cursor.lastrowid),
    )
    db.commit()
    return {"value": code, "label": cleaned, "kind": "custom"}, None

"""Shared operations for client directory membership."""

from .constants import CLIENT_RELATIONSHIP_TYPES, CLIENT_SEGMENTS


def normalize_phone_identity(phone):
    """Return a stable Russian phone identity without making phone required."""
    digits = "".join(character for character in str(phone or "") if character.isdigit())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def _normalized_name(name):
    return " ".join(str(name or "").split()).casefold()


def ensure_segment(db, client_id, segment, created_at):
    if segment not in CLIENT_SEGMENTS:
        raise ValueError("Unknown client segment")
    db.execute(
        "INSERT OR IGNORE INTO client_segments (client_id, segment, created_at) "
        "VALUES (?, ?, ?)",
        (client_id, segment, created_at),
    )


def create_directory_contact(
    db,
    *,
    name,
    phone,
    email,
    comment,
    segment,
    relationship_type,
    created_at,
    token,
):
    """Create a directory contact or safely reuse one from another segment.

    Phone is optional. When it is present, it is used only if it resolves to
    one contact with the same name. This prevents two people sharing a number
    or legacy duplicate data from being silently merged.
    """
    if segment not in CLIENT_SEGMENTS:
        raise ValueError("Неизвестный раздел клиентской базы.")
    if relationship_type not in CLIENT_RELATIONSHIP_TYPES:
        raise ValueError("Неизвестный тип контакта.")

    phone_identity = normalize_phone_identity(phone)
    matches = []
    if phone_identity:
        matches = [
            row
            for row in db.execute(
                "SELECT id, client_name, phone, email, comment FROM clients "
                "WHERE TRIM(COALESCE(phone, '')) != '' ORDER BY id"
            ).fetchall()
            if normalize_phone_identity(row["phone"]) == phone_identity
        ]
    if len(matches) > 1:
        raise ValueError(
            "Этот телефон уже указан у нескольких контактов. "
            "Найдите нужную карточку через поиск и проверьте данные."
        )

    if matches:
        existing = matches[0]
        if _normalized_name(existing["client_name"]) != _normalized_name(name):
            raise ValueError(
                f"Телефон уже принадлежит контакту «{existing['client_name']}». "
                "Откройте существующую карточку или уточните номер."
            )
        membership = db.execute(
            "SELECT relationship_type FROM client_segments "
            "WHERE client_id = ? AND segment = ?",
            (existing["id"], segment),
        ).fetchone()
        if membership is not None:
            if membership["relationship_type"] != relationship_type:
                current_label = (
                    "партнёров"
                    if membership["relationship_type"] == "partner"
                    else "клиентов"
                )
                raise ValueError(
                    f"Контакт уже находится в этом разделе среди {current_label}. "
                    "Измените его тип в существующей карточке."
                )
            return {"client_id": existing["id"], "result": "existing"}

        db.execute(
            "UPDATE clients SET "
            "email = CASE WHEN TRIM(COALESCE(email, '')) = '' THEN ? ELSE email END, "
            "comment = CASE WHEN TRIM(COALESCE(comment, '')) = '' THEN ? ELSE comment END "
            "WHERE id = ?",
            (email, comment, existing["id"]),
        )
        db.execute(
            "INSERT INTO client_segments "
            "(client_id, segment, relationship_type, created_at) "
            "VALUES (?, ?, ?, ?)",
            (existing["id"], segment, relationship_type, created_at),
        )
        return {"client_id": existing["id"], "result": "linked"}

    cursor = db.execute(
        "INSERT INTO clients "
        "(client_name, boat_model, phone, token, status, created_at, email, comment) "
        "VALUES (?, '', ?, ?, 'neutral', ?, ?, ?)",
        (name, phone, token, created_at, email, comment),
    )
    db.execute(
        "INSERT INTO client_segments "
        "(client_id, segment, relationship_type, created_at) "
        "VALUES (?, ?, ?, ?)",
        (cursor.lastrowid, segment, relationship_type, created_at),
    )
    return {"client_id": cursor.lastrowid, "result": "created"}


def update_directory_contact(
    db,
    *,
    client_id,
    name,
    phone,
    email,
    comment,
    updated_at,
):
    """Update one shared contact and its denormalized operational copies.

    Orders, schedule participants and diagnostic sheets keep a readable copy
    of the customer's name and phone. Updating them together prevents the
    partner directory and related documents from showing conflicting data.
    """
    current = db.execute(
        "SELECT id, client_name, phone FROM clients WHERE id = ?", (client_id,)
    ).fetchone()
    if current is None:
        raise ValueError("Партнёр не найден.")

    current_phone_identity = normalize_phone_identity(current["phone"])
    new_phone_identity = normalize_phone_identity(phone)
    if new_phone_identity and new_phone_identity != current_phone_identity:
        matches = [
            row
            for row in db.execute(
                "SELECT id, client_name, phone FROM clients "
                "WHERE id != ? AND TRIM(COALESCE(phone, '')) != '' ORDER BY id",
                (client_id,),
            ).fetchall()
            if normalize_phone_identity(row["phone"]) == new_phone_identity
        ]
        if matches:
            raise ValueError(
                f"Телефон уже принадлежит контакту «{matches[0]['client_name']}». "
                "Укажите другой номер или оставьте поле пустым."
            )

    identity_changed = (
        name != current["client_name"] or phone != (current["phone"] or "")
    )
    db.execute(
        "UPDATE clients SET client_name = ?, phone = ?, email = ?, comment = ? "
        "WHERE id = ?",
        (name, phone, email, comment, client_id),
    )
    if not identity_changed:
        return

    db.execute(
        "UPDATE tuning_orders SET client_name = ?, phone = ?, updated_at = ? "
        "WHERE client_id = ?",
        (name, phone, updated_at, client_id),
    )
    db.execute(
        "UPDATE schedule_items SET customer_name = ?, customer_phone = ?, "
        "updated_at = ? WHERE kind = 'booking' AND id IN ("
        "SELECT schedule_item_id FROM schedule_participants WHERE client_id = ?)",
        (name, phone, updated_at, client_id),
    )
    db.execute(
        "UPDATE schedule_participants SET client_name = ?, client_phone = ? "
        "WHERE client_id = ?",
        (name, phone, client_id),
    )
    db.execute(
        "UPDATE field_diagnostic_sheets SET owner_name = ?, owner_phone = ? "
        "WHERE owner_client_id = ?",
        (name, phone, client_id),
    )

"""Initial and repeatable synchronization of the YCLIENTS client directory."""

import math
import secrets

import requests

from .constants import EXCURSION_SEGMENT
from .services import ensure_segment


PAGE_SIZE = 200


def _text(value, limit):
    return " ".join(str(value or "").strip().split())[:limit]


def _phone_identity(phone):
    digits = "".join(character for character in str(phone or "") if character.isdigit())
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits


def _client_name(remote):
    display_name = _text(remote.get("display_name"), 180)
    if display_name:
        return display_name
    full_name = " ".join(
        part for part in (
            _text(remote.get("surname"), 80),
            _text(remote.get("name"), 80),
            _text(remote.get("patronymic"), 80),
        ) if part
    )
    return _text(full_name, 180)


def fetch_clients(
    api_base,
    company_id,
    partner_token,
    user_token,
    http_session=None,
    timeout=30,
):
    """Fetch every client page before making any local database changes."""
    if not all((company_id, partner_token, user_token)):
        raise RuntimeError("Не настроены токены или идентификатор YCLIENTS.")
    session = http_session or requests.Session()
    headers = {
        "Accept": "application/vnd.yclients.v2+json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {partner_token}, User {user_token}",
    }
    result = []
    page = 1
    total_count = None
    while True:
        response = session.get(
            f"{api_base}/clients/{company_id}",
            headers=headers,
            params={"page": page, "count": PAGE_SIZE},
            timeout=timeout,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"YCLIENTS вернул HTTP {response.status_code} при загрузке клиентов."
            )
        try:
            body = response.json()
        except ValueError as error:
            raise RuntimeError("YCLIENTS вернул некорректный JSON.") from error
        if not body.get("success"):
            raise RuntimeError("YCLIENTS вернул success=false при загрузке клиентов.")
        rows = body.get("data") or []
        if not isinstance(rows, list):
            raise RuntimeError("YCLIENTS вернул некорректный список клиентов.")
        result.extend(rows)
        meta = body.get("meta") or {}
        if total_count is None:
            try:
                total_count = int(meta.get("total_count"))
            except (TypeError, ValueError):
                total_count = None
        if not rows or len(rows) < PAGE_SIZE:
            break
        if total_count is not None and len(result) >= total_count:
            break
        page += 1
        if page > 10_000 or (
            total_count is not None and page > math.ceil(total_count / PAGE_SIZE) + 1
        ):
            raise RuntimeError("Не удалось завершить пагинацию клиентов YCLIENTS.")
    return result[:total_count] if total_count is not None else result


def import_clients(db, remote_clients, timestamp):
    """Upsert by YCLIENTS id, merging only unambiguous legacy identities."""
    local_rows = [
        dict(row) for row in db.execute(
            "SELECT id, client_name, phone, yclients_client_id FROM clients "
            "ORDER BY id"
        ).fetchall()
    ]
    by_remote_id = {
        row["yclients_client_id"]: row
        for row in local_rows if row["yclients_client_id"] is not None
    }
    by_phone = {}
    by_name_without_phone = {}
    for row in local_rows:
        identity = _phone_identity(row["phone"])
        if identity:
            by_phone.setdefault(identity, []).append(row)
        elif row["yclients_client_id"] is None:
            by_name_without_phone.setdefault(
                row["client_name"].strip().casefold(), []
            ).append(row)

    stats = {"received": len(remote_clients), "created": 0, "updated": 0,
             "linked": 0, "skipped": 0}
    for remote in remote_clients:
        try:
            remote_id = int(remote.get("id"))
        except (TypeError, ValueError):
            stats["skipped"] += 1
            continue
        name = _client_name(remote)
        if not name:
            stats["skipped"] += 1
            continue
        phone = _text(remote.get("phone"), 50)
        email = _text(remote.get("email"), 180)
        birth_date = _text(remote.get("birth_date"), 30)
        comment = str(remote.get("comment") or "").strip()[:4000]
        last_change = _text(remote.get("last_change_date"), 50)

        local = by_remote_id.get(remote_id)
        was_linked = False
        if local is None:
            phone_matches = by_phone.get(_phone_identity(phone), []) if phone else []
            available_phone_matches = [
                row for row in phone_matches
                if row["yclients_client_id"] is None
            ]
            if len(available_phone_matches) == 1:
                local = available_phone_matches[0]
                was_linked = True
            elif not phone:
                name_matches = by_name_without_phone.get(name.casefold(), [])
                if len(name_matches) == 1:
                    local = name_matches[0]
                    was_linked = True

        if local is None:
            cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at, "
                "yclients_client_id, email, birth_date, comment, "
                "yclients_last_change_date) VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name, phone, secrets.token_urlsafe(16), timestamp, remote_id,
                    email, birth_date, comment, last_change,
                ),
            )
            client_id = cursor.lastrowid
            local = {
                "id": client_id, "client_name": name, "phone": phone,
                "yclients_client_id": remote_id,
            }
            local_rows.append(local)
            stats["created"] += 1
        else:
            client_id = local["id"]
            db.execute(
                "UPDATE clients SET client_name = ?, phone = ?, "
                "yclients_client_id = ?, email = ?, birth_date = ?, comment = ?, "
                "yclients_last_change_date = ? WHERE id = ?",
                (
                    name, phone or local["phone"], remote_id, email, birth_date,
                    comment, last_change, client_id,
                ),
            )
            local["client_name"] = name
            local["phone"] = phone or local["phone"]
            local["yclients_client_id"] = remote_id
            stats["linked" if was_linked else "updated"] += 1

        by_remote_id[remote_id] = local
        identity = _phone_identity(local["phone"])
        if identity and local not in by_phone.setdefault(identity, []):
            by_phone[identity].append(local)
        ensure_segment(db, client_id, EXCURSION_SEGMENT, timestamp)
    return stats


def sync_clients(
    db,
    timestamp,
    api_base,
    company_id,
    partner_token,
    user_token,
    http_session=None,
):
    remote_clients = fetch_clients(
        api_base,
        company_id,
        partner_token,
        user_token,
        http_session=http_session,
    )
    try:
        stats = import_clients(db, remote_clients, timestamp)
        db.commit()
        return stats
    except Exception:
        db.rollback()
        raise

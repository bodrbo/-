"""Rules of the client's equipment: form parsing and the cabinet context."""

import datetime as dt

from . import repository
from .constants import DEFAULT_KIND, KINDS, NAME_MAX, NOTE_MAX, SERIAL_MAX


def parse_form(form):
    """-> (data, error)."""
    name = " ".join((form.get("name") or "").split())
    if not name:
        return None, "Укажите модель техники."
    kind = form.get("kind") or DEFAULT_KIND
    if kind not in KINDS:
        kind = DEFAULT_KIND
    year_raw = (form.get("year") or "").strip()
    year = None
    if year_raw:
        if not year_raw.isdigit() or not 1950 <= int(year_raw) <= dt.date.today().year + 1:
            return None, "Год выпуска указан неверно."
        year = int(year_raw)
    return {
        "kind": kind,
        "name": name[:NAME_MAX],
        "serial_number": " ".join((form.get("serial_number") or "").split())[:SERIAL_MAX],
        "year": year,
        "note": (form.get("note") or "").strip()[:NOTE_MAX],
    }, None


def add_item(db, client_id, form):
    data, error = parse_form(form)
    if error:
        return False, error
    repository.add(db, client_id, data, dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    db.commit()
    return True, "Техника добавлена."


def update_item(db, item_id, form):
    data, error = parse_form(form)
    if error:
        return False, error
    repository.update(db, item_id, data)
    db.commit()
    return True, "Изменения сохранены."


def delete_item(db, item_id):
    repository.delete(db, item_id)
    db.commit()
    return True, "Техника удалена."


def cabinet_context(db, client_id, with_catalog):
    """Rows for the cabinet block; the catalog suggestions only for the editor."""
    items = [
        dict(row, kind_label=KINDS.get(row["kind"], ""))
        for row in repository.list_for_client(db, client_id)
    ]
    return {
        "items": items,
        "kinds": KINDS,
        "catalog": repository.catalog_names(db) if with_catalog else {},
    }

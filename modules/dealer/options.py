"""Boat option catalog and immutable option snapshots in sold equipment."""

import io
import json
import os
import re
import secrets
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


def catalog(db):
    return [dict(r) for r in db.execute(
        "SELECT o.*, p.model_name AS boat_name FROM dealer_boat_options o "
        "LEFT JOIN tuning_boat_profiles p ON p.id = o.profile_id "
        "ORDER BY o.active DESC, o.name COLLATE NOCASE, o.id"
    ).fetchall()]


def snapshots(item):
    try:
        value = json.loads(item.get("options_json") or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def item_view(row):
    item = dict(row)
    item["options"] = snapshots(item)
    # The stored selling price includes options, so all existing consumers
    # (cabinet, payments, analytics) continue to see the full equipment price.
    item["base_price"] = round(item["price"] - sum(o["price"] for o in item["options"]), 2)
    return item


def parse_selection(raw, kind, profile_id, name, existing, available):
    """Client submits IDs only. Prices and details come from trusted storage."""
    try:
        ids = json.loads(raw or "[]")
        if not isinstance(ids, list) or len(ids) > 100 or any(type(i) is not int for i in ids):
            raise ValueError
        if len(ids) != len(set(ids)):
            raise ValueError
    except (TypeError, ValueError):
        return "Некорректный список доп. опций.", []
    if ids and kind != "boat":
        return "Доп. опции можно добавить только к лодке.", []
    same_boat = existing and existing["kind"] == "boat" and existing["profile_id"] == profile_id
    if same_boat and profile_id is None:
        same_boat = existing["name"] == name
    old = {o["id"]: o for o in snapshots(existing)} if same_boat else {}
    chosen = []
    for option_id in ids:
        if option_id in old:
            chosen.append(old[option_id])
            continue
        option = available.get(option_id)
        if not option or not option["active"] or option["profile_id"] not in (None, profile_id):
            return "Выбранная доп. опция недоступна для этой лодки. Обновите выбор.", []
        chosen.append({key: option[key] for key in (
            "id", "profile_id", "name", "description", "price", "photo_filename",
        )})
    return None, chosen


def image_bytes(upload):
    """Decode a bounded raster and re-encode it; never serve uploaded HTML/SVG."""
    raw = upload.read(5 * 1024 * 1024 + 1)
    if not raw or len(raw) > 5 * 1024 * 1024:
        raise ValueError("Фотография должна быть не больше 5 МБ.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as photo:
                if photo.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("Загрузите фотографию JPG, PNG или WebP.")
                if photo.width * photo.height > 20_000_000:
                    raise ValueError("Фотография слишком большая — не более 20 мегапикселей.")
                photo.load()
                photo = ImageOps.exif_transpose(photo)
                photo.thumbnail((2000, 2000))
                output = io.BytesIO()
                photo.convert("RGB").save(output, format="JPEG", quality=90)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("Не удалось прочитать фотографию. Загрузите JPG, PNG или WebP.") from None


def valid_photo_name(filename):
    return bool(re.fullmatch(r"[0-9a-f]{32}\.jpg", filename))


def save(db, form, upload, directory, option_id=None):
    from .services import _number, now_text

    existing = db.execute("SELECT * FROM dealer_boat_options WHERE id = ?", (option_id,)).fetchone()
    if option_id is not None and existing is None:
        return "Доп. опция не найдена."
    name = " ".join(form.get("name", "").split())
    description = form.get("description", "").strip()
    price = _number(form.get("price"))
    profile_raw = form.get("profile_id", "").strip()
    profile_id = int(profile_raw) if profile_raw.isdigit() else None
    if not name or len(name) > 200:
        return "Укажите название доп. опции — до 200 символов."
    if not description or len(description) > 4000:
        return "Добавьте описание — до 4000 символов."
    if price is None or price < 0:
        return "Укажите стоимость — число не меньше нуля."
    if profile_raw and (profile_id is None or db.execute(
        "SELECT id FROM tuning_boat_profiles WHERE id = ? AND equipment_type = 'boat'", (profile_id,)
    ).fetchone() is None):
        return "Выберите лодку из каталога или вариант «Все лодки»."
    filename = existing["photo_filename"] if existing else ""
    contents = None
    if upload and upload.filename:
        try:
            contents = image_bytes(upload)
        except ValueError as error:
            return str(error)
        filename = secrets.token_hex(16) + ".jpg"
    if not filename:
        return "Добавьте фотографию доп. опции."
    path = None
    try:
        if contents:
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, filename)
            with open(path, "xb") as target:
                target.write(contents)
        stamp = now_text()
        if existing:
            db.execute(
                "UPDATE dealer_boat_options SET name=?, description=?, price=?, profile_id=?, "
                "photo_filename=?, updated_at=? WHERE id=?",
                (name, description, round(price, 2), profile_id, filename, stamp, option_id),
            )
        else:
            db.execute(
                "INSERT INTO dealer_boat_options (name, description, price, profile_id, photo_filename, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, description, round(price, 2), profile_id, filename, stamp, stamp),
            )
        db.commit()
    except Exception:
        db.rollback()
        if path and os.path.isfile(path):
            os.remove(path)
        raise
    # Old images remain available to snapshots of previously saved orders.
    return None

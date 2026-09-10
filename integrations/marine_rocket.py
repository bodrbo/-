"""Marine Rocket YML catalog adapter and idempotent stock synchronizer."""

import datetime as dt
import xml.etree.ElementTree as ET

import requests


DEFAULT_YML_URL = (
    "https://dealers.marinerocket.ru/uploads/yml/"
    "f2e1ee4bb8b3895bb069ea2fe1bcb61f33e3c15c/export.yml"
)
SOURCE_KEY = "marine_rocket"
SUPPLIER_NAME = "Marine Rocket"
MOTOR_ROOT_CATEGORY_ID = "2238869"
WAREHOUSE_LOCATIONS = ("Москва", "Владивосток")
MAX_YML_BYTES = 5 * 1024 * 1024

DESCRIPTION_FIELDS = (
    "Тактность",
    "Мощность двигателя",
    "Количество цилиндров",
    "Система запуска",
    "Управление",
    "Подъем",
    "Объем двигателя",
    "Подача топлива",
    "Высота транца",
    "Вес",
    "Гарантия производителя",
)


class MarineRocketError(RuntimeError):
    """A recoverable feed, parsing or warehouse-mapping error."""


def fetch_yml(url=DEFAULT_YML_URL, timeout=(5, 25)):
    """Download the public supplier feed with a strict size limit."""
    try:
        response = requests.get(url, timeout=timeout, stream=True)
    except requests.RequestException as error:
        raise MarineRocketError(
            "не удалось связаться с сервером Marine Rocket"
        ) from error
    if not response.ok:
        raise MarineRocketError(
            "сервер Marine Rocket вернул HTTP {}".format(response.status_code)
        )
    try:
        declared_size = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared_size = 0
    if declared_size > MAX_YML_BYTES:
        raise MarineRocketError("выгрузка Marine Rocket превышает допустимый размер")
    chunks = []
    received_size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        received_size += len(chunk)
        if received_size > MAX_YML_BYTES:
            raise MarineRocketError("выгрузка Marine Rocket превышает допустимый размер")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise MarineRocketError("Marine Rocket вернул пустую выгрузку")
    return content


def _clean_text(element, child_name):
    value = element.findtext(child_name)
    return str(value or "").strip()


def _number(value, field_name):
    try:
        number = float(str(value or "0").strip().replace(",", "."))
    except (TypeError, ValueError) as error:
        raise MarineRocketError(
            "некорректное значение поля {} в YML".format(field_name)
        ) from error
    if number < 0:
        raise MarineRocketError(
            "отрицательное значение поля {} в YML".format(field_name)
        )
    return number


def _category_is_below(category_id, parent_by_id):
    visited = set()
    while category_id and category_id not in visited:
        if category_id == MOTOR_ROOT_CATEGORY_ID:
            return True
        visited.add(category_id)
        category_id = parent_by_id.get(category_id)
    return False


def _offer_parameters(offer):
    parameters = {}
    for parameter in offer.findall("param"):
        name = str(parameter.attrib.get("name") or "").strip()
        value = str(parameter.text or "").strip()
        if name and value and name not in parameters:
            parameters[name] = value
    return parameters


def parse_motor_offers(content):
    """Return normalized offers from the motor category tree only.

    The feed also contains more than a thousand spare parts. Category ancestry,
    rather than a name substring, keeps those out of the supply catalog.
    Repeated quantity nodes for the same city are summed: the supplier uses
    them for separate physical stock locations with the same public label.
    """
    if not content:
        raise MarineRocketError("Marine Rocket вернул пустую выгрузку")
    if len(content) > MAX_YML_BYTES:
        raise MarineRocketError("выгрузка Marine Rocket превышает допустимый размер")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise MarineRocketError("Marine Rocket вернул некорректный XML") from error

    categories = root.findall(".//categories/category")
    parent_by_id = {
        str(category.attrib.get("id") or "").strip():
        str(category.attrib.get("parentId") or "").strip() or None
        for category in categories
    }
    category_names = {
        str(category.attrib.get("id") or "").strip():
        str(category.text or "").strip()
        for category in categories
    }
    if MOTOR_ROOT_CATEGORY_ID not in parent_by_id:
        raise MarineRocketError("в выгрузке отсутствует раздел моторов Marine Rocket")

    result = []
    seen_refs = set()
    for offer in root.findall(".//offers/offer"):
        category_id = _clean_text(offer, "categoryId")
        if not _category_is_below(category_id, parent_by_id):
            continue

        external_ref = str(offer.attrib.get("id") or "").strip()
        name = _clean_text(offer, "name")
        if not external_ref or not name:
            raise MarineRocketError("у моторной позиции отсутствует ID или название")
        if external_ref in seen_refs:
            raise MarineRocketError(
                "в выгрузке повторяется ID моторной позиции {}".format(external_ref)
            )
        seen_refs.add(external_ref)

        parameters = _offer_parameters(offer)
        sku = parameters.get("articul") or _clean_text(offer, "barcode") or None
        description_parts = []
        category_name = category_names.get(category_id)
        if category_name:
            description_parts.append(category_name)
        for field_name in DESCRIPTION_FIELDS:
            value = parameters.get(field_name)
            if value:
                description_parts.append("{}: {}".format(field_name, value))

        quantities = {location: 0.0 for location in WAREHOUSE_LOCATIONS}
        if str(offer.attrib.get("available") or "true").lower() != "false":
            for quantity in offer.findall("quantity"):
                location = str(quantity.attrib.get("location") or "").strip()
                if location in quantities:
                    quantities[location] += _number(
                        quantity.text, "остаток ({})".format(location)
                    )

        result.append({
            "external_ref": external_ref,
            "name": name,
            "sku": sku,
            "description": "; ".join(description_parts) or None,
            "cost_price": _number(_clean_text(offer, "dealer_price"), "dealer_price"),
            "sale_price": _number(_clean_text(offer, "price"), "price"),
            "external_url": _clean_text(offer, "url") or None,
            "external_photo_url": _clean_text(offer, "picture") or None,
            "quantities": quantities,
        })

    if not result:
        raise MarineRocketError("в выгрузке не найдено ни одного мотора")
    return result


def _resolve_warehouses(db):
    warehouses = [dict(row) for row in db.execute(
        "SELECT id, name FROM supply_warehouses ORDER BY id"
    ).fetchall()]
    resolved = {}
    for location in WAREHOUSE_LOCATIONS:
        normalized = location.casefold()
        exact = [row for row in warehouses if row["name"].strip().casefold() == normalized]
        candidates = exact or [
            row for row in warehouses if normalized in row["name"].casefold()
        ]
        if not candidates:
            raise MarineRocketError(
                "не найден склад, в названии которого есть «{}»".format(location)
            )
        if len(candidates) > 1:
            raise MarineRocketError(
                "найдено несколько складов для «{}»; оставьте город только в названии нужного склада"
                .format(location)
            )
        resolved[location] = candidates[0]["id"]
    if len(set(resolved.values())) != len(resolved):
        raise MarineRocketError("Москва и Владивосток должны быть разными складами")
    return resolved


def _replace_stock_quantity(db, product_id, warehouse_id, quantity):
    """Portable upsert for Beget's older SQLite build."""
    existing = db.execute(
        "SELECT id FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
        (product_id, warehouse_id),
    ).fetchone()
    if existing is None:
        db.execute(
            "INSERT INTO supply_stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)",
            (product_id, warehouse_id, quantity),
        )
    else:
        db.execute(
            "UPDATE supply_stock SET quantity = ? WHERE id = ?",
            (quantity, existing["id"]),
        )


def sync_motor_catalog(db, content, now=None):
    """Upsert product cards and replace supplier warehouse balances.

    Existing manually-created products are adopted only by an exact SKU match
    and only when they are not already linked to another external source.
    Products missing from a later complete feed remain in the catalog, but their
    Marine Rocket warehouse balances are reset to zero.
    """
    offers = parse_motor_offers(content)
    warehouse_ids = _resolve_warehouses(db)
    updated_at = (now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    created = 0
    updated = 0
    adopted = 0
    active_product_ids = set()

    try:
        for offer in offers:
            product = db.execute(
                "SELECT sp.* FROM supply_product_external_links link "
                "JOIN supply_products sp ON sp.id = link.product_id "
                "WHERE link.source = ? AND link.external_ref = ?",
                (SOURCE_KEY, offer["external_ref"]),
            ).fetchone()
            if product is None:
                product = db.execute(
                    "SELECT * FROM supply_products WHERE external_source = ? AND external_ref = ?",
                    (SOURCE_KEY, offer["external_ref"]),
                ).fetchone()
            was_adopted = False
            if product is None and offer["sku"]:
                product = db.execute(
                    "SELECT * FROM supply_products WHERE sku = ? "
                    "AND (external_source IS NULL OR external_source = '') ORDER BY id LIMIT 1",
                    (offer["sku"],),
                ).fetchone()
                was_adopted = product is not None

            values = (
                offer["name"], offer["sku"], offer["description"], SUPPLIER_NAME,
                offer["cost_price"], "piece", offer["sale_price"], SOURCE_KEY,
                offer["external_ref"], offer["external_url"],
                offer["external_photo_url"], updated_at,
            )
            if product is None:
                cursor = db.execute(
                    "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, "
                    "cost_price, cost_unit, sale_price, min_stock, created_at, external_source, "
                    "external_ref, external_url, external_photo_url, external_updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                    values[:7] + (updated_at,) + values[7:],
                )
                product_id = cursor.lastrowid
                created += 1
            else:
                product_id = product["id"]
                db.execute(
                    "UPDATE supply_products SET name = ?, sku = ?, description = ?, supplier = ?, "
                    "cost_price = ?, cost_unit = ?, sale_price = ?, external_source = ?, "
                    "external_ref = ?, external_url = ?, external_photo_url = ?, "
                    "external_updated_at = ? WHERE id = ?",
                    values + (product_id,),
                )
                updated += 1
                if was_adopted:
                    adopted += 1
            link = db.execute(
                "SELECT product_id FROM supply_product_external_links "
                "WHERE source = ? AND external_ref = ?",
                (SOURCE_KEY, offer["external_ref"]),
            ).fetchone()
            link_values = (
                product_id, offer["external_url"], offer["external_photo_url"],
                updated_at, SOURCE_KEY, offer["external_ref"],
            )
            if link is None:
                db.execute(
                    "INSERT INTO supply_product_external_links "
                    "(product_id, external_url, external_photo_url, external_updated_at, source, external_ref) "
                    "VALUES (?, ?, ?, ?, ?, ?)", link_values,
                )
            else:
                db.execute(
                    "UPDATE supply_product_external_links SET product_id = ?, external_url = ?, "
                    "external_photo_url = ?, external_updated_at = ? "
                    "WHERE source = ? AND external_ref = ?", link_values,
                )
            active_product_ids.add(product_id)

            for location, warehouse_id in warehouse_ids.items():
                _replace_stock_quantity(
                    db, product_id, warehouse_id, offer["quantities"][location]
                )

        stale_rows = db.execute(
            "SELECT DISTINCT product_id AS id FROM supply_product_external_links WHERE source = ?",
            (SOURCE_KEY,),
        ).fetchall()
        stale_ids = [row["id"] for row in stale_rows if row["id"] not in active_product_ids]
        for product_id in stale_ids:
            for warehouse_id in warehouse_ids.values():
                _replace_stock_quantity(db, product_id, warehouse_id, 0)
        db.commit()
    except Exception:
        db.rollback()
        raise

    totals = {}
    for location, warehouse_id in warehouse_ids.items():
        totals[location] = db.execute(
            "SELECT COALESCE(SUM(ss.quantity), 0) AS total "
            "FROM supply_stock ss WHERE ss.warehouse_id = ? AND EXISTS ("
            "SELECT 1 FROM supply_product_external_links link "
            "WHERE link.product_id = ss.product_id AND link.source = ?)",
            (warehouse_id, SOURCE_KEY),
        ).fetchone()["total"]
    return {
        "received": len(offers),
        "created": created,
        "updated": updated,
        "adopted": adopted,
        "stale": len(stale_ids),
        "totals": totals,
        "updated_at": updated_at,
    }

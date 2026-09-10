"""1000 размеров YML catalog adapter and idempotent stock synchronizer."""

import datetime as dt
import io
import xml.etree.ElementTree as ET

import requests


DEFAULT_YML_URL = (
    "https://opt.1000size.ru/uploads/yml/"
    "88e3cb8a8a1750025e9e5a838b52f764a04a65ed/export.yml"
)
SOURCE_KEY = "thousand_sizes"
SUPPLIER_NAME = "1000 размеров"
WAREHOUSE_NAMES = {
    "Москва": "1000 размеров - Москва",
    "Владивосток": "1000 размеров - Владивосток",
}
MAX_YML_BYTES = 64 * 1024 * 1024


class ThousandSizesError(RuntimeError):
    """A recoverable supplier feed or synchronization error."""


def fetch_yml(url=DEFAULT_YML_URL, timeout=(10, 180)):
    """Download the public feed with a limit large enough for the full catalog."""
    try:
        response = requests.get(url, timeout=timeout, stream=True)
    except requests.RequestException as error:
        raise ThousandSizesError("не удалось связаться с сервером 1000 размеров") from error
    if not response.ok:
        raise ThousandSizesError(
            "сервер 1000 размеров вернул HTTP {}".format(response.status_code)
        )
    try:
        declared_size = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        declared_size = 0
    if declared_size > MAX_YML_BYTES:
        raise ThousandSizesError("выгрузка 1000 размеров превышает допустимый размер")
    chunks = []
    received_size = 0
    for chunk in response.iter_content(chunk_size=256 * 1024):
        if not chunk:
            continue
        received_size += len(chunk)
        if received_size > MAX_YML_BYTES:
            raise ThousandSizesError("выгрузка 1000 размеров превышает допустимый размер")
        chunks.append(chunk)
    content = b"".join(chunks)
    if not content:
        raise ThousandSizesError("1000 размеров вернул пустую выгрузку")
    return content


def _number(value, field_name):
    try:
        number = float(str(value or "0").strip().replace(",", "."))
    except (TypeError, ValueError) as error:
        raise ThousandSizesError(
            "некорректное значение поля {} в YML".format(field_name)
        ) from error
    if number < 0:
        raise ThousandSizesError("отрицательное значение поля {} в YML".format(field_name))
    return number


def _text(element, child_name):
    return str(element.findtext(child_name) or "").strip()


def _category_path(category_id, names, parents):
    parts = []
    visited = set()
    while category_id and category_id not in visited:
        visited.add(category_id)
        if names.get(category_id):
            parts.append(names[category_id])
        category_id = parents.get(category_id)
    return tuple(reversed(parts))


def parse_offers(content):
    """Parse every offer and retain only the two requested city balances."""
    if not content:
        raise ThousandSizesError("1000 размеров вернул пустую выгрузку")
    if len(content) > MAX_YML_BYTES:
        raise ThousandSizesError("выгрузка 1000 размеров превышает допустимый размер")
    category_names = {}
    category_parents = {}
    offers = []
    seen_refs = set()
    try:
        iterator = ET.iterparse(io.BytesIO(content), events=("end",))
        for _, element in iterator:
            if element.tag == "category":
                category_id = str(element.attrib.get("id") or "").strip()
                if category_id:
                    category_names[category_id] = str(element.text or "").strip()
                    category_parents[category_id] = (
                        str(element.attrib.get("parentId") or "").strip() or None
                    )
                element.clear()
                continue
            if element.tag != "offer":
                continue
            external_ref = str(element.attrib.get("id") or "").strip()
            name = _text(element, "name")
            if not external_ref or not name:
                raise ThousandSizesError("у товарной позиции отсутствует ID или название")
            if external_ref in seen_refs:
                raise ThousandSizesError(
                    "в выгрузке повторяется ID товарной позиции {}".format(external_ref)
                )
            seen_refs.add(external_ref)
            parameters = {}
            for parameter in element.findall("param"):
                parameter_name = str(parameter.attrib.get("name") or "").strip()
                parameter_value = str(parameter.text or "").strip()
                if parameter_name and parameter_value and parameter_name not in parameters:
                    parameters[parameter_name] = parameter_value
            sku = parameters.get("articul") or _text(element, "barcode") or None
            category_id = _text(element, "categoryId")
            description = []
            category_path = _category_path(category_id, category_names, category_parents)
            if category_path:
                description.append("Категория: {}".format(" / ".join(category_path)))
            vendor = _text(element, "vendor")
            if vendor:
                description.append("Производитель: {}".format(vendor))
            raw_description = " ".join(_text(element, "description").split())
            if raw_description:
                description.append(raw_description)
            quantities = {location: 0.0 for location in WAREHOUSE_NAMES}
            if str(element.attrib.get("available") or "true").lower() != "false":
                for quantity in element.findall("quantity"):
                    location = str(quantity.attrib.get("location") or "").strip()
                    if location in quantities:
                        quantities[location] += _number(
                            quantity.text, "остаток ({})".format(location)
                        )
            offers.append({
                "external_ref": external_ref,
                "name": name,
                "sku": sku,
                "description": "; ".join(description)[:8000] or None,
                "cost_price": _number(_text(element, "dealer_price"), "dealer_price"),
                "sale_price": _number(_text(element, "price"), "price"),
                "external_url": _text(element, "url") or None,
                "external_photo_url": _text(element, "picture") or None,
                "category_path": category_path,
                "quantities": quantities,
            })
            element.clear()
    except ET.ParseError as error:
        raise ThousandSizesError("1000 размеров вернул некорректный XML") from error
    if not offers:
        raise ThousandSizesError("в выгрузке не найдено ни одного товара")
    return offers


def _ensure_warehouses(db, now):
    resolved = {}
    for location, warehouse_name in WAREHOUSE_NAMES.items():
        row = db.execute(
            "SELECT id FROM supply_warehouses WHERE CASEFOLD(name) = ? ORDER BY id LIMIT 1",
            (warehouse_name.casefold(),),
        ).fetchone()
        if row is None:
            cursor = db.execute(
                "INSERT INTO supply_warehouses (name, address, created_at) VALUES (?, NULL, ?)",
                (warehouse_name, now),
            )
            resolved[location] = cursor.lastrowid
        else:
            resolved[location] = row["id"]
    return resolved


def _replace_stock_quantity(db, product_id, warehouse_id, quantity):
    row = db.execute(
        "SELECT id FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
        (product_id, warehouse_id),
    ).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO supply_stock (product_id, warehouse_id, quantity) VALUES (?, ?, ?)",
            (product_id, warehouse_id, quantity),
        )
    else:
        db.execute("UPDATE supply_stock SET quantity = ? WHERE id = ?", (quantity, row["id"]))


def _ensure_categories(db, offers, now):
    """Mirror category paths below one supplier root and return path -> id."""
    root_name = SUPPLIER_NAME
    root = db.execute(
        "SELECT id FROM supply_categories WHERE parent_id IS NULL "
        "AND CASEFOLD(name) = ? ORDER BY id LIMIT 1",
        (root_name.casefold(),),
    ).fetchone()
    if root is None:
        root_id = db.execute(
            "INSERT INTO supply_categories (name, parent_id, created_at) VALUES (?, NULL, ?)",
            (root_name, now),
        ).lastrowid
    else:
        root_id = root["id"]
    resolved = {(): root_id}
    paths = set()
    for offer in offers:
        path = offer.get("category_path") or ()
        for length in range(1, len(path) + 1):
            paths.add(path[:length])
    for path in sorted(paths, key=lambda item: (len(item), tuple(p.casefold() for p in item))):
        parent_id = resolved[path[:-1]]
        row = db.execute(
            "SELECT id FROM supply_categories WHERE parent_id = ? AND CASEFOLD(name) = ? "
            "ORDER BY id LIMIT 1",
            (parent_id, path[-1].casefold()),
        ).fetchone()
        if row is None:
            category_id = db.execute(
                "INSERT INTO supply_categories (name, parent_id, created_at) VALUES (?, ?, ?)",
                (path[-1], parent_id, now),
            ).lastrowid
        else:
            category_id = row["id"]
        resolved[path] = category_id
    return resolved


def sync_catalog(db, content, now=None):
    """Upsert the full supplier catalog and replace both warehouse balances."""
    offers = parse_offers(content)
    updated_at = (now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    warehouse_ids = _ensure_warehouses(db, updated_at)
    category_ids = _ensure_categories(db, offers, updated_at)
    created = updated = adopted = 0
    active_product_ids = set()
    try:
        for offer in offers:
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
                offer["cost_price"], "piece", offer["sale_price"],
                category_ids[offer.get("category_path") or ()], SOURCE_KEY,
                offer["external_ref"], offer["external_url"],
                offer["external_photo_url"], updated_at,
            )
            if product is None:
                cursor = db.execute(
                    "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, "
                    "cost_price, cost_unit, sale_price, category_id, min_stock, created_at, external_source, "
                    "external_ref, external_url, external_photo_url, external_updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                    values[:8] + (updated_at,) + values[8:],
                )
                product_id = cursor.lastrowid
                created += 1
            else:
                product_id = product["id"]
                db.execute(
                    "UPDATE supply_products SET name = ?, sku = ?, description = ?, supplier = ?, "
                    "cost_price = ?, cost_unit = ?, sale_price = ?, "
                    "category_id = COALESCE(category_id, ?), external_source = ?, "
                    "external_ref = ?, external_url = ?, external_photo_url = ?, "
                    "external_updated_at = ? WHERE id = ?",
                    values + (product_id,),
                )
                updated += 1
                if was_adopted:
                    adopted += 1
            active_product_ids.add(product_id)
            for location, warehouse_id in warehouse_ids.items():
                _replace_stock_quantity(
                    db, product_id, warehouse_id, offer["quantities"][location]
                )

        stale_rows = db.execute(
            "SELECT id FROM supply_products WHERE external_source = ?", (SOURCE_KEY,)
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
            "SELECT COALESCE(SUM(ss.quantity), 0) AS total FROM supply_stock ss "
            "JOIN supply_products sp ON sp.id = ss.product_id "
            "WHERE ss.warehouse_id = ? AND sp.external_source = ?",
            (warehouse_id, SOURCE_KEY),
        ).fetchone()["total"]
    return {
        "received": len(offers), "created": created, "updated": updated,
        "adopted": adopted, "stale": len(stale_ids), "totals": totals,
        "updated_at": updated_at,
    }

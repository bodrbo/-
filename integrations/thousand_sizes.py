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


def _identity_key(value):
    """Case-insensitive key with repeated whitespace normalized."""
    return " ".join(str(value or "").split()).casefold()


def _merge_products(db, winner_id, duplicate_id):
    """Move all product history to winner and remove the duplicate card."""
    if winner_id == duplicate_id:
        return
    # Keep manually uploaded imagery and catalog settings when only one of the
    # two cards has them. Supplier fields themselves are refreshed afterwards.
    db.execute(
        "UPDATE supply_products SET "
        "photo_filename = COALESCE(photo_filename, (SELECT photo_filename FROM supply_products WHERE id = ?)), "
        "min_stock = COALESCE(min_stock, (SELECT min_stock FROM supply_products WHERE id = ?)), "
        "category_id = COALESCE(category_id, (SELECT category_id FROM supply_products WHERE id = ?)) "
        "WHERE id = ?",
        (duplicate_id, duplicate_id, duplicate_id, winner_id),
    )
    duplicate_stock = db.execute(
        "SELECT * FROM supply_stock WHERE product_id = ?", (duplicate_id,)
    ).fetchall()
    for row in duplicate_stock:
        winner_stock = db.execute(
            "SELECT id FROM supply_stock WHERE product_id = ? AND warehouse_id = ?",
            (winner_id, row["warehouse_id"]),
        ).fetchone()
        if winner_stock is None:
            db.execute(
                "UPDATE supply_stock SET product_id = ? WHERE id = ?",
                (winner_id, row["id"]),
            )
        else:
            db.execute(
                "UPDATE supply_stock SET quantity = quantity + ? WHERE id = ?",
                (row["quantity"], winner_stock["id"]),
            )
            db.execute("DELETE FROM supply_stock WHERE id = ?", (row["id"],))
    for table_name in (
        "supply_receipts", "supply_writeoffs", "supply_requests",
        "tuning_order_products", "supply_product_external_links",
    ):
        db.execute(
            "UPDATE {} SET product_id = ? WHERE product_id = ?".format(table_name),
            (winner_id, duplicate_id),
        )
    db.execute("DELETE FROM supply_products WHERE id = ?", (duplicate_id,))


def _upsert_external_link(db, product_id, offer, updated_at):
    existing = db.execute(
        "SELECT product_id FROM supply_product_external_links "
        "WHERE source = ? AND external_ref = ?",
        (SOURCE_KEY, offer["external_ref"]),
    ).fetchone()
    values = (
        product_id, offer["external_url"], offer["external_photo_url"], updated_at,
        SOURCE_KEY, offer["external_ref"],
    )
    if existing is None:
        db.execute(
            "INSERT INTO supply_product_external_links "
            "(product_id, external_url, external_photo_url, external_updated_at, source, external_ref) "
            "VALUES (?, ?, ?, ?, ?, ?)", values,
        )
    else:
        db.execute(
            "UPDATE supply_product_external_links SET product_id = ?, external_url = ?, "
            "external_photo_url = ?, external_updated_at = ? "
            "WHERE source = ? AND external_ref = ?", values,
        )


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
    """Upsert feed and merge cards matching either normalized name or SKU."""
    offers = parse_offers(content)
    updated_at = (now or dt.datetime.now()).strftime("%Y-%m-%d %H:%M")
    warehouse_ids = _ensure_warehouses(db, updated_at)
    category_ids = _ensure_categories(db, offers, updated_at)
    created = updated = adopted = merged = 0
    active_product_ids = set()
    feed_quantities = {}
    products = {row["id"]: dict(row) for row in db.execute(
        "SELECT * FROM supply_products ORDER BY id"
    ).fetchall()}
    initial_product_ids = set(products)
    names = {}
    skus = {}

    def add_to_indexes(product):
        name_key = _identity_key(product.get("name"))
        sku_key = _identity_key(product.get("sku"))
        if name_key:
            names.setdefault(name_key, set()).add(product["id"])
        if sku_key:
            skus.setdefault(sku_key, set()).add(product["id"])

    def remove_from_indexes(product):
        for index, key in ((names, _identity_key(product.get("name"))),
                           (skus, _identity_key(product.get("sku")))):
            if key and key in index:
                index[key].discard(product["id"])
                if not index[key]:
                    del index[key]

    for product in products.values():
        add_to_indexes(product)
    source_links = {
        row["external_ref"]: row["product_id"]
        for row in db.execute(
            "SELECT external_ref, product_id FROM supply_product_external_links WHERE source = ?",
            (SOURCE_KEY,),
        ).fetchall()
    }
    try:
        for offer in offers:
            mapped_id = source_links.get(offer["external_ref"])
            candidate_ids = set()
            candidate_ids.update(names.get(_identity_key(offer["name"]), set()))
            if offer["sku"]:
                candidate_ids.update(skus.get(_identity_key(offer["sku"]), set()))
            if mapped_id in products:
                candidate_ids.add(mapped_id)
            winner_id = mapped_id if mapped_id in products else (
                min(candidate_ids) if candidate_ids else None
            )
            was_adopted = (
                winner_id is not None and mapped_id is None
                and winner_id in initial_product_ids
            )
            if winner_id is not None:
                for duplicate_id in sorted(candidate_ids - {winner_id}):
                    duplicate = products.get(duplicate_id)
                    if duplicate is None:
                        continue
                    _merge_products(db, winner_id, duplicate_id)
                    remove_from_indexes(duplicate)
                    del products[duplicate_id]
                    for reference, product_id in list(source_links.items()):
                        if product_id == duplicate_id:
                            source_links[reference] = winner_id
                    for location in WAREHOUSE_NAMES:
                        duplicate_key = (duplicate_id, location)
                        if duplicate_key in feed_quantities:
                            winner_key = (winner_id, location)
                            feed_quantities[winner_key] = (
                                feed_quantities.get(winner_key, 0)
                                + feed_quantities.pop(duplicate_key)
                            )
                    merged += 1
            product = products.get(winner_id) if winner_id is not None else None
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
                product = {
                    "id": product_id, "name": offer["name"], "sku": offer["sku"],
                    "external_source": SOURCE_KEY, "external_ref": offer["external_ref"],
                }
                products[product_id] = product
                add_to_indexes(product)
            else:
                product_id = product["id"]
                remove_from_indexes(product)
                db.execute(
                    "UPDATE supply_products SET name = ?, sku = COALESCE(?, sku), description = ?, supplier = ?, "
                    "cost_price = ?, cost_unit = ?, sale_price = ?, "
                    "category_id = COALESCE(category_id, ?), "
                    "external_url = COALESCE(external_url, ?), "
                    "external_photo_url = COALESCE(external_photo_url, ?), "
                    "external_updated_at = ? WHERE id = ?",
                    values[:8] + (offer["external_url"], offer["external_photo_url"],
                                  updated_at, product_id),
                )
                if not product.get("external_source"):
                    db.execute(
                        "UPDATE supply_products SET external_source = ?, external_ref = ? WHERE id = ?",
                        (SOURCE_KEY, offer["external_ref"], product_id),
                    )
                    product["external_source"] = SOURCE_KEY
                    product["external_ref"] = offer["external_ref"]
                product["name"] = offer["name"]
                if offer["sku"]:
                    product["sku"] = offer["sku"]
                add_to_indexes(product)
                updated += 1
                if was_adopted:
                    adopted += 1
            _upsert_external_link(db, product_id, offer, updated_at)
            source_links[offer["external_ref"]] = product_id
            active_product_ids.add(product_id)
            for location in warehouse_ids:
                key = (product_id, location)
                feed_quantities[key] = (
                    feed_quantities.get(key, 0) + offer["quantities"][location]
                )

        for product_id in active_product_ids:
            for location, warehouse_id in warehouse_ids.items():
                _replace_stock_quantity(
                    db, product_id, warehouse_id,
                    feed_quantities.get((product_id, location), 0),
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
            "SELECT COALESCE(SUM(ss.quantity), 0) AS total FROM supply_stock ss "
            "WHERE ss.warehouse_id = ? AND EXISTS ("
            "SELECT 1 FROM supply_product_external_links link "
            "WHERE link.product_id = ss.product_id AND link.source = ?)",
            (warehouse_id, SOURCE_KEY),
        ).fetchone()["total"]
    return {
        "received": len(offers), "created": created, "updated": updated,
        "adopted": adopted, "merged": merged, "stale": len(stale_ids), "totals": totals,
        "updated_at": updated_at,
    }

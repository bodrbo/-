import unittest
from unittest import mock

from support import application_module
from integrations.thousand_sizes import parse_offers, sync_catalog


def feed(first_moscow="2", first_price="1200", include_second=True):
    second = """
      <offer id="item-2" available="false">
        <name>Второй товар</name><param name="articul">TS-002</param>
        <price>2500</price><dealer_price>2100</dealer_price>
        <categoryId>child</categoryId><quantity location="Москва">99</quantity>
      </offer>
    """ if include_second else ""
    return ("""<?xml version="1.0" encoding="UTF-8"?>
    <yml_catalog><shop><categories>
      <category id="root">Оснащение</category>
      <category id="child" parentId="root">Якорное оборудование</category>
    </categories><offers>
      <offer id="item-1" available="true">
        <name>Рым-болт</name><param name="articul">TS-001</param>
        <vendor>Youthful</vendor><price>{price}</price><dealer_price>900</dealer_price>
        <url>https://opt.example/item-1</url><picture>https://opt.example/item-1.jpg</picture>
        <categoryId>child</categoryId>
        <quantity location="Москва">{moscow}</quantity>
        <quantity location="Москва">3</quantity>
        <quantity location="Владивосток">4</quantity>
        <quantity location="Химки">100</quantity>
      </offer>{second}
    </offers></shop></yml_catalog>""").format(
        price=first_price, moscow=first_moscow, second=second
    ).encode("utf-8")


class ThousandSizesSyncTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM supply_stock")
            db.execute("DELETE FROM supply_products")
            db.execute("DELETE FROM supply_categories")
            db.execute("DELETE FROM supply_warehouses")
            db.execute("DELETE FROM supply_external_sync_state")
            db.commit()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM supply_stock")
            db.execute("DELETE FROM supply_products")
            db.execute("DELETE FROM supply_categories")
            db.execute("DELETE FROM supply_warehouses")
            db.execute("DELETE FROM supply_external_sync_state")
            db.commit()

    def test_parser_reads_full_catalog_and_only_requested_city_balances(self):
        offers = parse_offers(feed())
        self.assertEqual(len(offers), 2)
        self.assertEqual(offers[0]["quantities"], {"Москва": 5, "Владивосток": 4})
        self.assertEqual(offers[1]["quantities"], {"Москва": 0, "Владивосток": 0})
        self.assertIn("Оснащение / Якорное оборудование", offers[0]["description"])
        self.assertEqual(offers[0]["cost_price"], 900)

    def test_sync_creates_exact_warehouses_and_is_idempotent(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            first = sync_catalog(db, feed())
            self.assertEqual(first["created"], 2)
            self.assertEqual(first["totals"], {"Москва": 5, "Владивосток": 4})
            names = [row["name"] for row in db.execute(
                "SELECT name FROM supply_warehouses ORDER BY name"
            ).fetchall()]
            self.assertEqual(names, ["1000 размеров - Владивосток", "1000 размеров - Москва"])
            product = db.execute(
                "SELECT sp.category_id, sc.name AS category_name FROM supply_products sp "
                "LEFT JOIN supply_categories sc ON sc.id = sp.category_id "
                "WHERE sp.external_ref = 'item-1'"
            ).fetchone()
            self.assertEqual(product["category_name"], "Якорное оборудование")

            second = sync_catalog(db, feed(first_moscow="7", first_price="1300", include_second=False))
            self.assertEqual(second["created"], 0)
            self.assertEqual(second["updated"], 1)
            self.assertEqual(second["stale"], 1)
            self.assertEqual(second["totals"], {"Москва": 10, "Владивосток": 4})
            self.assertEqual(db.execute(
                "SELECT COUNT(*) AS n FROM supply_products"
            ).fetchone()["n"], 2)

    def test_manual_card_is_adopted_by_exact_sku(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO supply_products (name, sku, cost_price, cost_unit, sale_price, created_at) "
                "VALUES ('Старое имя', 'TS-001', 1, 'piece', 2, '2026-09-01 10:00')"
            )
            db.commit()
            stats = sync_catalog(db, feed(include_second=False))
            self.assertEqual(stats["adopted"], 1)
            row = db.execute("SELECT * FROM supply_products").fetchone()
            self.assertEqual(row["external_source"], "thousand_sizes")
            self.assertEqual(row["name"], "Рым-болт")

    def test_admin_manual_sync_and_paginated_search(self):
        with mock.patch.object(
            application_module, "fetch_thousand_sizes_yml", return_value=feed(include_second=False)
        ):
            response = self.client.post("/supply/catalog/1000-sizes/sync", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("1000 размеров обновлён", body)
        self.assertIn("Москва — 5 шт.", body)
        search = self.client.get("/supply/catalog?q=TS-001")
        self.assertEqual(search.status_code, 200)
        self.assertIn("Рым-болт", search.get_data(as_text=True))

    def test_routes_require_admin_and_cron_secret(self):
        anonymous = application_module.app.test_client()
        self.assertEqual(anonymous.post("/supply/catalog/1000-sizes/sync").status_code, 302)
        original_secret = application_module.CRON_SECRET
        application_module.CRON_SECRET = "sizes-secret"
        self.addCleanup(setattr, application_module, "CRON_SECRET", original_secret)
        self.assertEqual(self.client.get("/internal/cron/sync-1000-sizes").status_code, 403)
        with mock.patch.object(
            application_module, "fetch_thousand_sizes_yml", return_value=feed(include_second=False)
        ):
            response = self.client.get("/internal/cron/sync-1000-sizes?token=sizes-secret")
        self.assertEqual(response.status_code, 200)
        self.assertIn("карточек — 1", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest import mock

from support import application_module
from integrations.marine_rocket import (
    MarineRocketError,
    parse_motor_offers,
    sync_motor_catalog,
)


def feed(first_moscow="2", first_price="120000", include_second=True):
    second = """
      <offer id="motor-2" available="false">
        <name>Мотор лодочный Marine Rocket MREF40</name>
        <param name="articul">MR-002</param>
        <price>250000</price><dealer_price>210000</dealer_price>
        <categoryId>two-stroke</categoryId>
        <quantity location="Москва">99</quantity>
      </offer>
    """ if include_second else ""
    return ("""<?xml version="1.0" encoding="UTF-8"?>
    <yml_catalog><shop>
      <categories>
        <category id="2238869">Моторы Marine Rocket</category>
        <category id="two-stroke" parentId="2238869">Двухтактные моторы</category>
        <category id="parts">Запчасти</category>
      </categories>
      <offers>
        <offer id="motor-1" available="true">
          <name>Мотор лодочный Marine Rocket MR9.9</name>
          <param name="articul">MR-001</param>
          <param group="Характеристики" name="Тактность">Двухтактный</param>
          <price>{price}</price><dealer_price>95000</dealer_price>
          <url>https://dealers.marinerocket.ru/mr99.html</url>
          <picture>https://static.marinerocket.ru/mr99.jpg</picture>
          <categoryId>two-stroke</categoryId>
          <quantity location="Москва">{moscow}</quantity>
          <quantity location="Москва">3</quantity>
          <quantity location="Владивосток">4</quantity>
        </offer>
        {second}
        <offer id="part-1" available="true">
          <name>Крыльчатка Marine Rocket</name>
          <price>1000</price><dealer_price>700</dealer_price>
          <categoryId>parts</categoryId>
          <quantity location="Москва">100</quantity>
        </offer>
      </offers>
    </shop></yml_catalog>""").format(
        price=first_price, moscow=first_moscow, second=second
    ).encode("utf-8")


class MarineRocketSyncTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM supply_stock")
            db.execute("DELETE FROM supply_products")
            db.execute("DELETE FROM supply_warehouses")
            db.execute("DELETE FROM supply_external_sync_state")
            db.executemany(
                "INSERT INTO supply_warehouses (name, address, created_at) VALUES (?, NULL, ?)",
                (
                    ("Marine Rocket Москва", "2026-09-10 10:00"),
                    ("Склад Владивосток", "2026-09-10 10:00"),
                ),
            )
            db.commit()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM supply_stock")
            db.execute("DELETE FROM supply_products")
            db.execute("DELETE FROM supply_warehouses")
            db.execute("DELETE FROM supply_external_sync_state")
            db.commit()

    def test_parser_keeps_only_motor_tree_and_sums_duplicate_locations(self):
        offers = parse_motor_offers(feed())

        self.assertEqual(len(offers), 2)
        self.assertEqual(offers[0]["external_ref"], "motor-1")
        self.assertEqual(offers[0]["quantities"]["Москва"], 5)
        self.assertEqual(offers[0]["quantities"]["Владивосток"], 4)
        self.assertEqual(offers[0]["cost_price"], 95000)
        self.assertEqual(offers[0]["sale_price"], 120000)
        self.assertIn("Тактность: Двухтактный", offers[0]["description"])
        self.assertEqual(offers[1]["quantities"]["Москва"], 0)

    def test_sync_creates_cards_and_replaces_both_warehouse_balances(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            first = sync_motor_catalog(db, feed(), now=application_module.dt.datetime(2026, 9, 10, 11, 0))
            self.assertEqual(first["created"], 2)
            self.assertEqual(first["totals"], {"Москва": 5, "Владивосток": 4})

            product = db.execute(
                "SELECT * FROM supply_products WHERE external_ref = 'motor-1'"
            ).fetchone()
            self.assertEqual(product["sku"], "MR-001")
            self.assertEqual(product["supplier"], "Marine Rocket")
            self.assertEqual(product["cost_price"], 95000)
            self.assertEqual(product["sale_price"], 120000)
            self.assertEqual(product["external_photo_url"], "https://static.marinerocket.ru/mr99.jpg")

            second = sync_motor_catalog(
                db, feed(first_moscow="7", first_price="125000", include_second=False),
                now=application_module.dt.datetime(2026, 9, 10, 12, 0),
            )
            self.assertEqual(second["created"], 0)
            self.assertEqual(second["updated"], 1)
            self.assertEqual(second["stale"], 1)
            self.assertEqual(second["totals"], {"Москва": 10, "Владивосток": 4})
            self.assertEqual(
                db.execute("SELECT COUNT(*) AS n FROM supply_products").fetchone()["n"], 2
            )
            stale_stock = db.execute(
                "SELECT SUM(quantity) AS total FROM supply_stock ss "
                "JOIN supply_products sp ON sp.id = ss.product_id "
                "WHERE sp.external_ref = 'motor-2'"
            ).fetchone()["total"]
            self.assertEqual(stale_stock, 0)
            self.assertEqual(
                db.execute(
                    "SELECT sale_price FROM supply_products WHERE external_ref = 'motor-1'"
                ).fetchone()["sale_price"],
                125000,
            )

    def test_existing_manual_card_is_adopted_by_sku(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO supply_products (name, sku, description, supplier, photo_filename, "
                "cost_price, cost_unit, sale_price, min_stock, created_at) "
                "VALUES ('Старое имя', 'MR-001', NULL, NULL, NULL, 1, 'piece', 2, NULL, "
                "'2026-09-01 10:00')"
            )
            db.commit()
            stats = sync_motor_catalog(db, feed(include_second=False))

            self.assertEqual(stats["created"], 0)
            self.assertEqual(stats["adopted"], 1)
            row = db.execute("SELECT * FROM supply_products").fetchone()
            self.assertEqual(row["external_ref"], "motor-1")
            self.assertEqual(row["name"], "Мотор лодочный Marine Rocket MR9.9")

    def test_missing_city_warehouse_aborts_without_products(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM supply_warehouses WHERE name LIKE '%Владивосток%'")
            db.commit()
            with self.assertRaises(MarineRocketError):
                sync_motor_catalog(db, feed())
            self.assertEqual(
                db.execute("SELECT COUNT(*) AS n FROM supply_products").fetchone()["n"], 0
            )

    def test_admin_can_run_manual_sync_and_catalog_shows_external_photo(self):
        with mock.patch.object(
            application_module, "fetch_marine_rocket_yml", return_value=feed(include_second=False)
        ):
            response = self.client.post(
                "/supply/catalog/marine-rocket/sync", follow_redirects=True
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Marine Rocket обновлён", body)
        self.assertIn("https://static.marinerocket.ru/mr99.jpg", body)
        self.assertIn("Москва — 5 шт.", body)

        status = self.client.get("/supply/catalog/marine-rocket/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["status"], "success")

    def test_manual_sync_requires_admin_login(self):
        anonymous = application_module.app.test_client()
        response = anonymous.post("/supply/catalog/marine-rocket/sync")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])

    def test_standalone_cron_is_secret_protected(self):
        original_secret = application_module.CRON_SECRET
        application_module.CRON_SECRET = "marine-test-secret"
        self.addCleanup(setattr, application_module, "CRON_SECRET", original_secret)
        self.assertEqual(
            self.client.get("/internal/cron/sync-marine-rocket").status_code, 403
        )
        with mock.patch.object(
            application_module, "fetch_marine_rocket_yml", return_value=feed(include_second=False)
        ):
            response = self.client.get(
                "/internal/cron/sync-marine-rocket?token=marine-test-secret"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("карточек — 1", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

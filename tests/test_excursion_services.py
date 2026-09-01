import sqlite3
import unittest

from support import application_module
from modules.excursion_services.schema import init_schema


class ExcursionServicesIntegrationTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM excursion_service_boat_prices WHERE service_id IN ("
                "SELECT id FROM excursion_services WHERE name IN "
                "('Ночной тестовый маршрут', 'ночной тестовый маршрут', "
                "'Тестовая индивидуальная экскурсия'))"
            )
            db.execute(
                "DELETE FROM excursion_services WHERE name IN "
                "('Ночной тестовый маршрут', 'ночной тестовый маршрут', "
                "'средний тур', 'Тестовая индивидуальная экскурсия')"
            )
            db.execute(
                "UPDATE excursion_services SET service_type = 'group', price = 0 "
                "WHERE name = 'Средний тур'"
            )
            db.execute(
                "DELETE FROM excursion_service_boat_prices WHERE service_id IN ("
                "SELECT id FROM excursion_services WHERE name = 'Средний тур')"
            )
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM excursion_service_boat_prices WHERE service_id IN ("
                "SELECT id FROM excursion_services WHERE name IN "
                "('Ночной тестовый маршрут', 'ночной тестовый маршрут', "
                "'Тестовая индивидуальная экскурсия'))"
            )
            db.execute(
                "DELETE FROM excursion_services WHERE name IN "
                "('Ночной тестовый маршрут', 'ночной тестовый маршрут', "
                "'средний тур', 'Тестовая индивидуальная экскурсия')"
            )
            db.execute(
                "UPDATE excursion_services SET service_type = 'group', price = 0 "
                "WHERE name = 'Средний тур'"
            )
            db.execute(
                "DELETE FROM excursion_service_boat_prices WHERE service_id IN ("
                "SELECT id FROM excursion_services WHERE name = 'Средний тур')"
            )
            db.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def test_services_require_admin_login(self):
        self.assertEqual(self.client.get("/services").status_code, 302)
        self.assertEqual(self.client.post("/services", data={}).status_code, 302)

    def test_bootstrap_catalog_is_visible(self):
        self.login()
        response = self.client.get("/services")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Экскурсионные продукты", html)
        self.assertIn("Малый тур", html)
        self.assertNotIn('value="Индивидуальная аренда 2 часа"', html)
        self.assertIn("Цена за гостя, ₽", html)
        self.assertIn("Групповая экскурсия", html)
        self.assertIn("Индивидуальная экскурсия", html)
        self.assertIn('name="service_type"', html)
        self.assertIn('data-pricing-type="group"', html)
        self.assertIn('data-pricing-type="individual"', html)
        self.assertIn('class="active"', html)
        individual_html = self.client.get(
            "/services?section=individual"
        ).get_data(as_text=True)
        self.assertIn("Индивидуальная аренда 2 часа", individual_html)
        self.assertIn("Цена за час по катерам", individual_html)
        self.assertNotIn('value="Малый тур"', individual_html)

    def test_bootstrap_does_not_restore_a_renamed_service(self):
        connection = sqlite3.connect(":memory:")
        init_schema(connection)
        connection.execute(
            "UPDATE excursion_services SET name = 'Обновлённый малый тур' "
            "WHERE name = 'Малый тур'"
        )
        init_schema(connection)
        original_count = connection.execute(
            "SELECT COUNT(*) FROM excursion_services WHERE name = 'Малый тур'"
        ).fetchone()[0]
        total_count = connection.execute(
            "SELECT COUNT(*) FROM excursion_services"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(original_count, 0)
        self.assertEqual(total_count, 8)

    def test_existing_catalog_is_classified_during_migration(self):
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE excursion_services ("
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
            "duration_hours REAL NOT NULL, price REAL NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO excursion_services VALUES (?, ?, ?, 0, '', '')",
            [
                (1, "Средний тур", 1.5),
                (2, "Индивидуальная аренда 2 часа", 2.0),
            ],
        )
        init_schema(connection)
        types = dict(connection.execute(
            "SELECT name, service_type FROM excursion_services"
        ).fetchall())
        connection.close()
        self.assertEqual(types["Средний тур"], "group")
        self.assertEqual(types["Индивидуальная аренда 2 часа"], "individual")

    def test_admin_can_create_and_update_service(self):
        self.login()
        response = self.client.post(
            "/services",
            data={
                "name": "Ночной тестовый маршрут",
                "service_type": "group",
                "hours": "2,5",
                "price": "3 500",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            service = application_module.get_db().execute(
                "SELECT * FROM excursion_services "
                "WHERE name = 'Ночной тестовый маршрут'"
            ).fetchone()
        self.assertIsNotNone(service)
        self.assertEqual(service["duration_hours"], 2.5)
        self.assertEqual(service["price"], 3500)

        response = self.client.post(
            f"/services/{service['id']}",
            data={
                "name": "Ночной тестовый маршрут",
                "service_type": "group",
                "hours": "3",
                "price": "4200",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            updated = application_module.get_db().execute(
                "SELECT * FROM excursion_services WHERE id = ?", (service["id"],)
            ).fetchone()
        self.assertEqual(updated["duration_hours"], 3)
        self.assertEqual(updated["price"], 4200)

    def test_admin_can_create_individual_service_with_boat_hourly_rates(self):
        self.login()
        boat_names = [boat["name"] for boat in application_module.BOATS]
        response = self.client.post(
            "/services",
            data={
                "name": "Тестовая индивидуальная экскурсия",
                "service_type": "individual",
                "hours": "2",
                "boat_name[]": boat_names,
                "boat_price[]": ["4000", "5500", "6200"],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("section=individual", response.headers["Location"])
        with application_module.app.app_context():
            db = application_module.get_db()
            service = db.execute(
                "SELECT * FROM excursion_services "
                "WHERE name = 'Тестовая индивидуальная экскурсия'"
            ).fetchone()
            prices = {
                row["boat"]: row["hourly_price"]
                for row in db.execute(
                    "SELECT boat, hourly_price FROM excursion_service_boat_prices "
                    "WHERE service_id = ?",
                    (service["id"],),
                ).fetchall()
            }
        self.assertEqual(service["service_type"], "individual")
        self.assertEqual(service["price"], 0)
        self.assertEqual(prices, dict(zip(boat_names, [4000, 5500, 6200])))

    def test_admin_can_change_service_type_in_both_directions(self):
        self.login()
        boat_names = [boat["name"] for boat in application_module.BOATS]
        with application_module.app.app_context():
            service = application_module.get_db().execute(
                "SELECT * FROM excursion_services WHERE name = 'Средний тур'"
            ).fetchone()

        to_individual = self.client.post(
            f"/services/{service['id']}",
            data={
                "name": "Средний тур",
                "service_type": "individual",
                "hours": "1.5",
                "boat_name[]": boat_names,
                "boat_price[]": ["4100", "5200", "6300"],
            },
        )
        self.assertEqual(to_individual.status_code, 302)
        self.assertIn("section=individual", to_individual.headers["Location"])
        with application_module.app.app_context():
            db = application_module.get_db()
            changed = db.execute(
                "SELECT service_type, price FROM excursion_services WHERE id = ?",
                (service["id"],),
            ).fetchone()
            rates = db.execute(
                "SELECT COUNT(*) FROM excursion_service_boat_prices "
                "WHERE service_id = ?",
                (service["id"],),
            ).fetchone()[0]
        self.assertEqual(changed["service_type"], "individual")
        self.assertEqual(changed["price"], 0)
        self.assertEqual(rates, len(boat_names))

        to_group = self.client.post(
            f"/services/{service['id']}",
            data={
                "name": "Средний тур",
                "service_type": "group",
                "hours": "1.5",
                "price": "2900",
            },
        )
        self.assertEqual(to_group.status_code, 302)
        self.assertIn("section=group", to_group.headers["Location"])
        with application_module.app.app_context():
            db = application_module.get_db()
            restored = db.execute(
                "SELECT service_type, price FROM excursion_services WHERE id = ?",
                (service["id"],),
            ).fetchone()
            rates = db.execute(
                "SELECT COUNT(*) FROM excursion_service_boat_prices "
                "WHERE service_id = ?",
                (service["id"],),
            ).fetchone()[0]
        self.assertEqual(restored["service_type"], "group")
        self.assertEqual(restored["price"], 2900)
        self.assertEqual(rates, 0)

    def test_duplicate_and_invalid_values_are_rejected(self):
        self.login()
        duplicate = self.client.post(
            "/services",
            data={"name": "средний тур", "service_type": "group", "hours": "1.5", "price": "2500"},
            follow_redirects=True,
        )
        self.assertIn(
            "Услуга с таким названием уже есть",
            duplicate.get_data(as_text=True),
        )
        invalid = self.client.post(
            "/services",
            data={"name": "Ночной тестовый маршрут", "service_type": "group", "hours": "0", "price": "-1"},
            follow_redirects=True,
        )
        invalid_html = invalid.get_data(as_text=True)
        self.assertIn("Длительность должна быть от", invalid_html)
        self.assertIn("Цена должна быть от", invalid_html)

    def test_schedule_receives_catalog_price_and_pricing_logic(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE excursion_services SET price = 2750 "
                "WHERE name = 'Средний тур'"
            )
            db.commit()
        html = self.client.get("/schedule?date=2026-09-12").get_data(as_text=True)
        self.assertIn('data-name="Средний тур"', html)
        self.assertIn('data-service-type="group"', html)
        self.assertIn('data-price="2750.0"', html)
        self.assertIn("selected.dataset.serviceType === 'group'", html)
        self.assertIn("scheduleSelectedHours()", html)
        self.assertIn("data-boat-prices=", html)


if __name__ == "__main__":
    unittest.main()

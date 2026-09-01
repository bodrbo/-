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
                "DELETE FROM excursion_services WHERE name IN "
                "('Ночной тестовый маршрут', 'ночной тестовый маршрут', "
                "'средний тур')"
            )
            db.execute(
                "UPDATE excursion_services SET price = 0 "
                "WHERE name = 'Средний тур'"
            )
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "DELETE FROM excursion_services WHERE name IN "
                "('Ночной тестовый маршрут', 'ночной тестовый маршрут', "
                "'средний тур')"
            )
            db.execute(
                "UPDATE excursion_services SET price = 0 "
                "WHERE name = 'Средний тур'"
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
        self.assertIn("Индивидуальная аренда 2 часа", html)
        self.assertIn("Цена за гостя, ₽", html)
        self.assertIn('class="active"', html)

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

    def test_admin_can_create_and_update_service(self):
        self.login()
        response = self.client.post(
            "/services",
            data={
                "name": "Ночной тестовый маршрут",
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

    def test_duplicate_and_invalid_values_are_rejected(self):
        self.login()
        duplicate = self.client.post(
            "/services",
            data={"name": "средний тур", "hours": "1.5", "price": "2500"},
            follow_redirects=True,
        )
        self.assertIn(
            "Услуга с таким названием уже есть",
            duplicate.get_data(as_text=True),
        )
        invalid = self.client.post(
            "/services",
            data={"name": "Ночной тестовый маршрут", "hours": "0", "price": "-1"},
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
        self.assertIn('data-price="2750.0"', html)
        self.assertIn("unitPrice * multiplier", html)
        self.assertIn("kind === 'event'", html)


if __name__ == "__main__":
    unittest.main()

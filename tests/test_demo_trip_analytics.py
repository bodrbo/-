import unittest

from support import application_module


class DemoTripAnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)

    def setUp(self):
        self.client = application_module.app.test_client()

    def log_in_demo(self, modules):
        with self.client.session_transaction() as demo_session:
            demo_session.clear()
            demo_session["demo_tenant_id"] = 902
            demo_session["demo_tenant_name"] = "Демо аналитики рейсов"
            demo_session["demo_tenant_db_path"] = application_module.DB_PATH
            demo_session["demo_tenant_modules"] = ",".join(modules)

    def test_excursion_demo_gets_read_only_trip_analytics(self):
        self.log_in_demo(["excursions"])

        response = self.client.get("/analytics/trips")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Аналитика по рейсам", html)
        self.assertNotIn(">Транзакции<", html)
        self.assertNotIn(">Проекты<", html)
        self.assertNotIn("Рейсы и инвесторы", html)
        self.assertNotIn('action="/trips/', html)
        self.assertNotIn('href="/trips', html)

    def test_excursion_demo_cannot_open_legacy_trip_workspace(self):
        self.log_in_demo(["excursions"])

        self.assertEqual(self.client.get("/trips").status_code, 404)
        self.assertEqual(self.client.post("/trips/add", data={}).status_code, 404)

    def test_trip_analytics_requires_excursion_module(self):
        self.log_in_demo(["analytics"])

        self.assertEqual(self.client.get("/analytics/trips").status_code, 404)
        self.assertEqual(self.client.get("/analytics").status_code, 200)

    def test_combined_demo_shows_all_analytics_subsections(self):
        self.log_in_demo(["analytics", "excursions"])

        response = self.client.get("/analytics")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn(">Транзакции<", html)
        self.assertIn(">Проекты<", html)
        self.assertIn("Аналитика по рейсам", html)
        self.assertNotIn("Рейсы и инвесторы", html)


if __name__ == "__main__":
    unittest.main()

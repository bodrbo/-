import os
import sqlite3
import tempfile
import unittest

from support import application_module


class DemoSchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        application_module.app.config.update(TESTING=True)
        handle, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(self.db_path)
        application_module._provision_demo_tenant_db(self.db_path)

        db = sqlite3.connect(self.db_path)
        db.execute(
            "INSERT INTO employees (name, created_at) "
            "VALUES ('Демо Капитан', '2026-09-22 10:00')"
        )
        # Recreate the state of a demo DB created before the latest schedule
        # changes. Either omission used to make /schedule return HTTP 500.
        db.execute("ALTER TABLE fleet_vessels DROP COLUMN capacity")
        db.execute("DROP TABLE schedule_manual_payments")
        db.commit()
        db.close()

        self.client = application_module.app.test_client()
        with self.client.session_transaction() as demo_session:
            demo_session.clear()
            demo_session["demo_tenant_id"] = 903
            demo_session["demo_tenant_name"] = "Старое демо"
            demo_session["demo_tenant_db_path"] = self.db_path
            demo_session["demo_tenant_modules"] = "excursions"

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_schedule_request_migrates_existing_demo_database(self):
        response = self.client.get("/schedule?date=2026-09-22")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("Расписание рейсов", html)
        self.assertNotIn('<nav class="sub-tabs">', html)
        self.assertNotIn(">Экскурсии</a>", html)
        with self.client.session_transaction() as demo_session:
            self.assertEqual(
                demo_session.get("demo_tenant_schema_revision"),
                application_module.DEMO_TENANT_SCHEMA_REVISION,
            )

        db = sqlite3.connect(self.db_path)
        vessel_columns = {
            row[1] for row in db.execute("PRAGMA table_info(fleet_vessels)")
        }
        manual_payments_table = db.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'schedule_manual_payments'"
        ).fetchone()
        employees = {
            row[0] for row in db.execute("SELECT name FROM employees")
        }
        admin_count = db.execute(
            "SELECT COUNT(*) FROM admin_accounts"
        ).fetchone()[0]
        investor_count = db.execute(
            "SELECT COUNT(*) FROM investors"
        ).fetchone()[0]
        db.close()

        self.assertIn("capacity", vessel_columns)
        self.assertIsNotNone(manual_payments_table)
        self.assertEqual(employees, {"Демо Капитан"})
        self.assertEqual(admin_count, 0)
        self.assertEqual(investor_count, 0)

    def test_schedule_subnav_is_visible_when_demo_has_both_modules(self):
        with self.client.session_transaction() as demo_session:
            demo_session["demo_tenant_modules"] = "excursions,tuning"

        response = self.client.get("/schedule?date=2026-09-22")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn('<nav class="sub-tabs">', html)
        self.assertIn(">Экскурсии</a>", html)
        self.assertIn(">Тюнинг</a>", html)


if __name__ == "__main__":
    unittest.main()

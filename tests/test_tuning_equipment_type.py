import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from werkzeug.datastructures import MultiDict

from support import application_module


class TuningEquipmentTypeTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            self._clear_tuning_data(application_module.get_db())
        self.addCleanup(self.cleanup_database)

    @staticmethod
    def _clear_tuning_data(db):
        db.execute("DELETE FROM tuning_order_items")
        db.execute("DELETE FROM tuning_boat_profiles")
        db.execute("DELETE FROM projects WHERE tuning_order_id IS NOT NULL")
        db.execute("DELETE FROM clients")
        db.execute("DELETE FROM tuning_orders")
        db.commit()

    @staticmethod
    def cleanup_database():
        with application_module.app.app_context():
            TuningEquipmentTypeTests._clear_tuning_data(application_module.get_db())

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    @staticmethod
    def valid_form(
        equipment_type="boat",
        boat_model="Salute 585 HT",
        motor_model="",
        boat_registration_number="",
        motor_serial_number="",
    ):
        return MultiDict([
            ("client_name", "Иван Петров"),
            ("equipment_type", equipment_type),
            ("boat_model", boat_model),
            ("boat_registration_number", boat_registration_number),
            ("motor_model", motor_model),
            ("motor_serial_number", motor_serial_number),
            ("phone", "+7 900 000-00-00"),
            ("sale_channel", "direct"),
            ("discount_type", "percent"),
            ("discount_value", "0"),
            ("work_name[]", "Диагностика"),
            ("cost_price[]", "1000"),
            ("multiplier[]", "2"),
            ("item_id[]", ""),
        ])

    def test_form_normalizes_boat_and_motor_orders(self):
        boat_errors, boat = application_module._process_tuning_form(
            self.valid_form(
                motor_model="Yamaha F150",
                boat_registration_number="Р 12-34 ЛО",
                motor_serial_number="STALE-MOTOR-SERIAL",
            )
        )
        motor_errors, motor = application_module._process_tuning_form(
            self.valid_form(
                "motor",
                boat_model="Скрытое старое значение",
                motor_model="Suzuki DF200",
                boat_registration_number="STALE-BOAT-NUMBER",
                motor_serial_number="SN-200-77",
            )
        )

        self.assertEqual(boat_errors, [])
        self.assertEqual(boat["equipment_type"], "boat")
        self.assertEqual(boat["boat_model"], "Salute 585 HT")
        self.assertEqual(boat["boat_registration_number"], "Р 12-34 ЛО")
        self.assertEqual(boat["motor_model"], "Yamaha F150")
        self.assertEqual(boat["motor_serial_number"], "")
        self.assertEqual(motor_errors, [])
        self.assertEqual(motor["equipment_type"], "motor")
        self.assertEqual(motor["boat_model"], "")
        self.assertEqual(motor["boat_registration_number"], "")
        self.assertEqual(motor["motor_model"], "Suzuki DF200")
        self.assertEqual(motor["motor_serial_number"], "SN-200-77")

    def test_conditional_model_validation(self):
        boat_errors, _ = application_module._process_tuning_form(
            self.valid_form("boat", boat_model="", motor_model="Yamaha F150")
        )
        motor_errors, _ = application_module._process_tuning_form(
            self.valid_form("motor", boat_model="Salute 585 HT", motor_model="")
        )

        self.assertIn("Укажите модель лодки.", boat_errors)
        self.assertIn("Укажите модель мотора.", motor_errors)

    def test_creation_form_searches_catalog_and_creates_new_boat_profile(self):
        self.login()
        existing = self.client.post(
            "/tuning/add", data=self.valid_form(boat_model="Salute 585 HT")
        )

        form = self.client.get("/tuning/add")
        form_html = form.get_data(as_text=True)

        self.assertEqual(existing.status_code, 302)
        self.assertEqual(form.status_code, 200)
        self.assertIn('role="combobox"', form_html)
        self.assertIn("Salute 585 HT", form_html)
        self.assertIn("Создать новую модель", form_html)

        created = self.client.post(
            "/tuning/add", data=self.valid_form(boat_model="Nimbus 305 Coupe")
        )

        self.assertEqual(created.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT model_name FROM tuning_boat_profiles "
                "WHERE model_key = 'nimbus 305 coupe'"
            ).fetchone()
            self.assertIsNotNone(profile)
            self.assertEqual(profile["model_name"], "Nimbus 305 Coupe")

    def test_motor_order_is_saved_but_not_added_to_boat_catalog(self):
        self.login()
        response = self.client.post(
            "/tuning/add",
            data=self.valid_form(
                "motor",
                boat_model="Не должна сохраниться",
                motor_model="Mercury F200",
                boat_registration_number="Не должен сохраниться",
                motor_serial_number="MRC-200-001",
            ),
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            order = db.execute("SELECT * FROM tuning_orders").fetchone()
            self.assertEqual(order["equipment_type"], "motor")
            self.assertEqual(order["boat_model"], "")
            self.assertEqual(order["boat_registration_number"], "")
            self.assertEqual(order["motor_model"], "Mercury F200")
            self.assertEqual(order["motor_serial_number"], "MRC-200-001")
            order_id = order["id"]

        orders_page = self.client.get("/tuning")
        orders_html = orders_page.get_data(as_text=True)
        catalog = self.client.get("/tuning/boats")

        self.assertIn("Mercury F200", orders_html)
        self.assertIn("MRC-200-001", orders_html)
        self.assertIn("Мотор", orders_html)
        self.assertNotIn("Mercury F200", catalog.get_data(as_text=True))
        edit_page = self.client.get(f"/tuning/edit/{order_id}")
        edit_html = edit_page.get_data(as_text=True)
        self.assertIn('name="equipment_type" value="motor"', edit_html)
        self.assertIn('value="Mercury F200"', edit_html)
        self.assertIn('value="MRC-200-001"', edit_html)
        self.assertNotIn("Открыть профиль ↗", edit_html)

    def test_boat_profile_keeps_motor_on_order_not_on_model_identity(self):
        self.login()
        response = self.client.post(
            "/tuning/add",
            data=self.valid_form(
                "boat",
                boat_model="Salute 585 HT",
                motor_model="Yamaha F150",
                boat_registration_number="Р 55-85 ЛО",
            ),
        )
        self.assertEqual(response.status_code, 302)

        catalog = self.client.get("/tuning/boats")
        catalog_html = catalog.get_data(as_text=True)
        with application_module.app.app_context():
            db = application_module.get_db()
            profiles = db.execute("SELECT * FROM tuning_boat_profiles").fetchall()
            order = db.execute("SELECT * FROM tuning_orders").fetchone()

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["model_name"], "Salute 585 HT")
        self.assertEqual(order["motor_model"], "Yamaha F150")
        self.assertEqual(order["boat_registration_number"], "Р 55-85 ЛО")
        self.assertIn("Salute 585 HT", catalog_html)
        self.assertNotIn("Yamaha F150", catalog_html)

        profile = self.client.get(f"/tuning/boats/{profiles[0]['id']}")
        profile_html = profile.get_data(as_text=True)
        self.assertIn("Salute 585 HT", profile_html)
        self.assertIn("Yamaha F150", profile_html)
        self.assertIn("Р 55-85 ЛО", profile_html)

    def test_edit_switches_identifier_fields_with_equipment_type(self):
        self.login()
        self.client.post(
            "/tuning/add",
            data=self.valid_form(
                "motor",
                motor_model="Honda BF150",
                motor_serial_number="HONDA-OLD-150",
            ),
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = db.execute("SELECT id FROM tuning_orders").fetchone()["id"]
            item_id = db.execute(
                "SELECT id FROM tuning_order_items WHERE order_id = ?", (order_id,)
            ).fetchone()["id"]

        updated_form = self.valid_form(
            "boat",
            boat_model="Бодрый 600",
            motor_model="Honda BF150",
            boat_registration_number="Р 60-00 ЛО",
            motor_serial_number="STALE-SERIAL",
        )
        updated_form.setlist("item_id[]", [str(item_id)])
        response = self.client.post(f"/tuning/edit/{order_id}", data=updated_form)

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            order = application_module.get_db().execute(
                "SELECT * FROM tuning_orders WHERE id = ?", (order_id,)
            ).fetchone()
            self.assertEqual(order["equipment_type"], "boat")
            self.assertEqual(order["boat_registration_number"], "Р 60-00 ЛО")
            self.assertEqual(order["motor_serial_number"], "")

    def test_existing_schema_migrates_orders_to_boat_type(self):
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.addCleanup(os.remove, database_path)
        connection = sqlite3.connect(database_path)
        connection.execute(
            "CREATE TABLE tuning_orders ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, client_name TEXT NOT NULL, "
            "boat_model TEXT NOT NULL, sale_channel TEXT NOT NULL, phone TEXT NOT NULL, "
            "discount_pct REAL NOT NULL DEFAULT 0, subtotal REAL NOT NULL, total REAL NOT NULL, "
            "status TEXT NOT NULL DEFAULT 'estimate', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO tuning_orders "
            "(client_name, boat_model, sale_channel, phone, subtotal, total, created_at, updated_at) "
            "VALUES ('Клиент', 'Legacy Boat', 'direct', '+7', 0, 0, '2026-01-01', '2026-01-01')"
        )
        connection.commit()
        connection.close()

        with patch.object(application_module, "DB_PATH", database_path):
            application_module.init_db()

        migrated = sqlite3.connect(database_path)
        row = migrated.execute(
            "SELECT equipment_type, boat_model, boat_registration_number, "
            "motor_model, motor_serial_number FROM tuning_orders"
        ).fetchone()
        profile = migrated.execute(
            "SELECT model_name FROM tuning_boat_profiles"
        ).fetchone()
        migrated.close()

        self.assertEqual(row, ("boat", "Legacy Boat", "", "", ""))
        self.assertEqual(profile[0], "Legacy Boat")


if __name__ == "__main__":
    unittest.main()

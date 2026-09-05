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
        db.execute(
            "DELETE FROM task_notification_deliveries WHERE assignment_type = 'tuning'"
        )
        db.execute("DELETE FROM tuning_item_assignments")
        db.execute("DELETE FROM tuning_order_items")
        db.execute("DELETE FROM tuning_boat_profiles")
        db.execute("DELETE FROM projects WHERE tuning_order_id IS NOT NULL")
        db.execute("DELETE FROM client_segments")
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
            ("order_date", "2026-06-15"),
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
        self.assertEqual(boat["order_date"], "2026-06-15")
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

    def test_tuning_order_allows_client_without_phone(self):
        self.login()
        form = self.valid_form()
        form.setlist("phone", [""])
        response = self.client.post("/tuning/add", data=form)
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            client = db.execute(
                "SELECT * FROM clients WHERE client_name = 'Иван Петров'"
            ).fetchone()
            order = db.execute("SELECT * FROM tuning_orders").fetchone()
        self.assertIsNotNone(client)
        self.assertEqual(client["phone"], "")
        self.assertEqual(order["order_date"], "2026-06-15")

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
        self.assertIn("data-combo-allow-custom", form_html)
        self.assertIn('class="combo-option"', form_html)
        self.assertIn("Salute 585 HT", form_html)
        self.assertIn("Создать новую модель", form_html)
        with application_module.app.app_context():
            db = application_module.get_db()
            segment = db.execute(
                "SELECT client_segments.segment FROM client_segments "
                "JOIN clients ON clients.id = client_segments.client_id "
                "WHERE clients.phone = '+7 900 000-00-00'"
            ).fetchone()
        self.assertEqual(segment["segment"], "tuning")

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

    def test_tuning_form_uses_only_tuning_clients(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            tuning_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) VALUES "
                "('Тюнинг Клиент', '', '+79990001111', 'tuning-picker', '2026-09-01 10:00')"
            ).lastrowid
            excursion_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) VALUES "
                "('Турист Экскурсий', '', '+79990002222', 'excursion-picker', '2026-09-01 10:00')"
            ).lastrowid
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at) "
                "VALUES (?, 'tuning', '2026-09-01 10:00')",
                (tuning_id,),
            )
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at) "
                "VALUES (?, 'excursion', '2026-09-01 10:00')",
                (excursion_id,),
            )
            db.commit()

        html = self.client.get("/tuning/add").get_data(as_text=True)
        self.assertIn("Тюнинг Клиент", html)
        self.assertIn("+79990001111", html)
        self.assertNotIn("Турист Экскурсий", html)
        self.assertNotIn("+79990002222", html)
        self.assertIn("data-tuning-client-combo", html)

    def test_assigning_tuning_work_notifies_the_employee_immediately(self):
        self.login()
        self.client.post("/tuning/add", data=self.valid_form())
        with application_module.app.app_context():
            db = application_module.get_db()
            order_id = db.execute("SELECT id FROM tuning_orders").fetchone()["id"]
            item_id = db.execute(
                "SELECT id FROM tuning_order_items WHERE order_id = ?", (order_id,)
            ).fetchone()["id"]
            employee = db.execute(
                "SELECT id FROM employees WHERE name = 'Дмитрий Тарусов'"
            ).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO employee_positions "
                "(employee_id, position, created_at) VALUES (?, 'Тюнингмэн', ?)",
                (employee["id"], "2026-08-29 12:00"),
            )
            db.commit()

        edit_page = self.client.get(f"/tuning/edit/{order_id}")
        self.assertIn(b'name="comment"', edit_page.data)

        with patch.object(
            application_module,
            "send_telegram_notification_to_employee",
            return_value="sent",
        ) as notification:
            response = self.client.post(
                f"/tuning/{order_id}/item/{item_id}/assign",
                data={
                    "employee_name": "Дмитрий Тарусов",
                    "rate": "2000",
                    "norm_hours": "1.5",
                    "comment": "Согласовать место установки с клиентом",
                },
            )

        self.assertEqual(response.status_code, 302)
        notification.assert_called_once()
        self.assertEqual(notification.call_args.args[1], "Дмитрий Тарусов")
        self.assertIn("Вам поручена задача", notification.call_args.args[2])
        self.assertIn("Диагностика", notification.call_args.args[2])
        self.assertIn(
            "Согласовать место установки с клиентом",
            notification.call_args.args[2],
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            delivery = db.execute(
                "SELECT notification_event FROM task_notification_deliveries "
                "WHERE assignment_type = 'tuning'"
            ).fetchone()
            assignment = db.execute(
                "SELECT comment FROM tuning_item_assignments"
            ).fetchone()
        self.assertEqual(delivery["notification_event"], "task.assigned")
        self.assertEqual(
            assignment["comment"], "Согласовать место установки с клиентом"
        )
        assigned_page = self.client.get(f"/tuning/edit/{order_id}")
        self.assertIn(
            "Согласовать место установки с клиентом".encode(), assigned_page.data
        )

    def test_motor_order_is_saved_in_separate_motor_catalog(self):
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
        boat_catalog = self.client.get("/tuning/boats")
        motor_catalog = self.client.get("/tuning/motors")

        self.assertIn("Mercury F200", orders_html)
        self.assertIn("MRC-200-001", orders_html)
        self.assertIn("Мотор", orders_html)
        self.assertNotIn("Mercury F200", boat_catalog.get_data(as_text=True))
        motor_html = motor_catalog.get_data(as_text=True)
        self.assertIn("Mercury F200", motor_html)
        self.assertIn("Каталог моторов", motor_html)
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT * FROM tuning_boat_profiles WHERE equipment_type = 'motor'"
            ).fetchone()
        self.assertIsNotNone(profile)
        self.assertEqual(profile["model_key"], "motor:mercury f200")
        profile_href = f'href="/tuning/motors/{profile["id"]}"'
        self.assertIn(profile_href, motor_html)
        self.assertIn(profile_href, orders_html)
        edit_page = self.client.get(f"/tuning/edit/{order_id}")
        edit_html = edit_page.get_data(as_text=True)
        self.assertIn('name="equipment_type" value="motor"', edit_html)
        self.assertIn('value="Mercury F200"', edit_html)
        self.assertIn('value="MRC-200-001"', edit_html)
        self.assertIn(profile_href, edit_html)
        self.assertIn("Открыть профиль ↗", edit_html)

    def test_profile_type_can_move_between_motor_and_boat_catalogs(self):
        self.login()
        self.client.post(
            "/tuning/add",
            data=self.valid_form(
                "motor",
                motor_model="Honda BF15",
                motor_serial_number="BF15-123",
            ),
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            profile_id = db.execute(
                "SELECT id FROM tuning_boat_profiles "
                "WHERE model_key = 'motor:honda bf15'"
            ).fetchone()["id"]
            order_id = db.execute("SELECT id FROM tuning_orders").fetchone()["id"]

        moved_to_boats = self.client.post(
            f"/tuning/equipment/{profile_id}/type",
            data={"equipment_type": "boat"},
        )
        self.assertEqual(moved_to_boats.status_code, 302)
        self.assertTrue(
            moved_to_boats.headers["Location"].endswith(f"/tuning/boats/{profile_id}")
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            order = db.execute(
                "SELECT * FROM tuning_orders WHERE id = ?", (order_id,)
            ).fetchone()
            client_row = db.execute(
                "SELECT boat_model FROM clients WHERE id = ?", (order["client_id"],)
            ).fetchone()
        self.assertEqual(profile["equipment_type"], "boat")
        self.assertEqual(profile["model_key"], "honda bf15")
        self.assertEqual(order["equipment_type"], "boat")
        self.assertEqual(order["boat_model"], "Honda BF15")
        self.assertEqual(order["motor_model"], "")
        self.assertEqual(order["motor_serial_number"], "")
        self.assertEqual(client_row["boat_model"], "Honda BF15")
        self.assertIn(
            "Honda BF15", self.client.get("/tuning/boats").get_data(as_text=True)
        )
        self.assertNotIn(
            "Honda BF15", self.client.get("/tuning/motors").get_data(as_text=True)
        )

        moved_to_motors = self.client.post(
            f"/tuning/equipment/{profile_id}/type",
            data={"equipment_type": "motor"},
        )
        self.assertEqual(moved_to_motors.status_code, 302)
        self.assertTrue(
            moved_to_motors.headers["Location"].endswith(f"/tuning/motors/{profile_id}")
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            order = db.execute(
                "SELECT * FROM tuning_orders WHERE id = ?", (order_id,)
            ).fetchone()
            client_row = db.execute(
                "SELECT boat_model FROM clients WHERE id = ?", (order["client_id"],)
            ).fetchone()
        self.assertEqual(profile["equipment_type"], "motor")
        self.assertEqual(profile["model_key"], "motor:honda bf15")
        self.assertEqual(order["equipment_type"], "motor")
        self.assertEqual(order["boat_model"], "")
        self.assertEqual(order["motor_model"], "Honda BF15")
        self.assertEqual(client_row["boat_model"], "")

    def test_profile_type_change_merges_same_named_destination_profile(self):
        self.login()
        self.client.post(
            "/tuning/add",
            data=self.valid_form("boat", boat_model="Mercury 15"),
        )
        self.client.post(
            "/tuning/add",
            data=self.valid_form("motor", motor_model="Mercury 15"),
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            profiles = {
                row["equipment_type"]: row["id"]
                for row in db.execute(
                    "SELECT id, equipment_type FROM tuning_boat_profiles"
                ).fetchall()
            }

        response = self.client.post(
            f"/tuning/equipment/{profiles['boat']}/type",
            data={"equipment_type": "motor"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith(
                f"/tuning/motors/{profiles['motor']}"
            )
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            profiles_after = db.execute(
                "SELECT * FROM tuning_boat_profiles"
            ).fetchall()
            orders = db.execute(
                "SELECT equipment_type, boat_model, motor_model FROM tuning_orders"
            ).fetchall()
        self.assertEqual(len(profiles_after), 1)
        self.assertEqual(profiles_after[0]["equipment_type"], "motor")
        self.assertEqual(profiles_after[0]["model_key"], "motor:mercury 15")
        self.assertTrue(all(row["equipment_type"] == "motor" for row in orders))
        self.assertTrue(all(row["boat_model"] == "" for row in orders))
        self.assertTrue(all(row["motor_model"] == "Mercury 15" for row in orders))

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
        connection.execute(
            "CREATE TABLE tuning_boat_profiles ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, model_key TEXT NOT NULL UNIQUE, "
            "model_name TEXT NOT NULL, photo_filename TEXT, "
            "specifications TEXT NOT NULL DEFAULT '', "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO tuning_boat_profiles "
            "(model_key, model_name, created_at, updated_at) "
            "VALUES ('legacy boat', 'Legacy Boat', '2026-01-01', '2026-01-01')"
        )
        connection.commit()
        connection.close()

        with patch.object(application_module, "DB_PATH", database_path):
            application_module.init_db()

        migrated = sqlite3.connect(database_path)
        row = migrated.execute(
            "SELECT equipment_type, boat_model, boat_registration_number, "
            "motor_model, motor_serial_number, order_date FROM tuning_orders"
        ).fetchone()
        profile = migrated.execute(
            "SELECT model_name, equipment_type FROM tuning_boat_profiles"
        ).fetchone()
        migrated.close()

        self.assertEqual(row, ("boat", "Legacy Boat", "", "", "", "2026-01-01"))
        self.assertEqual(profile, ("Legacy Boat", "boat"))


if __name__ == "__main__":
    unittest.main()

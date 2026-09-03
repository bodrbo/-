import io
import os
import tempfile
import unittest

from support import application_module


class TuningBoatCatalogTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        self.upload_directory = tempfile.TemporaryDirectory()
        self.original_static_folder = application_module.app.static_folder
        application_module.app.static_folder = self.upload_directory.name
        self.addCleanup(self.upload_directory.cleanup)
        self.addCleanup(self.cleanup_database)
        self.addCleanup(
            setattr,
            application_module.app,
            "static_folder",
            self.original_static_folder,
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM tuning_order_items")
            db.execute("DELETE FROM tuning_boat_profiles")
            db.execute("DELETE FROM tuning_orders")
            db.commit()

    @staticmethod
    def cleanup_database():
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM tuning_order_items")
            db.execute("DELETE FROM tuning_boat_profiles")
            db.execute("DELETE FROM tuning_orders")
            db.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    @staticmethod
    def create_order(db, model, client, total, created_at, works=()):
        cursor = db.execute(
            "INSERT INTO tuning_orders "
            "(client_name, boat_model, sale_channel, phone, discount_pct, "
            "subtotal, total, status, created_at, updated_at) "
            "VALUES (?, ?, 'direct', '+7 900 000-00-00', 0, ?, ?, "
            "'done', ?, ?)",
            (client, model, total, total, created_at, created_at),
        )
        for work in works:
            db.execute(
                "INSERT INTO tuning_order_items "
                "(order_id, work_name, cost_price, multiplier, price, status) "
                "VALUES (?, ?, 0, 1, 0, 'done')",
                (cursor.lastrowid, work),
            )
        return cursor.lastrowid

    def test_catalog_groups_orders_into_one_profile_per_normalized_model(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            older_id = self.create_order(
                db,
                "Salute 585 HT",
                "Первый клиент",
                50000,
                "2026-07-01 10:00",
                ("Установка эхолота",),
            )
            newer_id = self.create_order(
                db,
                "  SALUTE   585 ht  ",
                "Второй клиент",
                75000,
                "2026-08-20 12:00",
                ("Монтаж электрики", "Установка лебёдки"),
            )
            self.create_order(db, "", "Заявка без модели", 0, "2026-08-21 12:00")
            db.commit()

        self.login()
        catalog = self.client.get("/tuning/boats")
        html = catalog.get_data(as_text=True)

        self.assertEqual(catalog.status_code, 200)
        self.assertIn("SALUTE 585 ht", html)
        self.assertIn("2 заказов", html)
        self.assertIn("125 000,00 ₽", html)
        with application_module.app.app_context():
            db = application_module.get_db()
            profiles = db.execute("SELECT * FROM tuning_boat_profiles").fetchall()
            self.assertEqual(len(profiles), 1)
            profile_id = profiles[0]["id"]

        orders_page = self.client.get("/tuning")
        orders_html = orders_page.get_data(as_text=True)
        profile_href = f'href="/tuning/boats/{profile_id}"'
        self.assertEqual(orders_page.status_code, 200)
        self.assertEqual(orders_html.count(profile_href), 2)

        order_page = self.client.get(f"/tuning/edit/{older_id}")
        order_html = order_page.get_data(as_text=True)
        self.assertEqual(order_page.status_code, 200)
        self.assertIn(profile_href, order_html)
        self.assertIn("Открыть профиль ↗", order_html)

        profile = self.client.get(f"/tuning/boats/{profile_id}")
        profile_html = profile.get_data(as_text=True)

        self.assertEqual(profile.status_code, 200)
        self.assertIn(f"№{older_id}", profile_html)
        self.assertIn(f"№{newer_id}", profile_html)
        self.assertIn("Установка эхолота", profile_html)
        self.assertIn("Монтаж электрики · Установка лебёдки", profile_html)
        self.assertNotIn("Заявка без модели", profile_html)

    def test_profile_accepts_characteristics_and_photo(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            self.create_order(
                db,
                "Axopar 28",
                "Клиент",
                100000,
                "2026-08-22 10:00",
            )
            application_module._sync_tuning_boat_profiles(db)
            db.commit()
            profile_id = db.execute(
                "SELECT id FROM tuning_boat_profiles WHERE model_key = 'axopar 28'"
            ).fetchone()["id"]

        self.login()
        response = self.client.post(
            f"/tuning/boats/{profile_id}/edit",
            data={
                "specifications": "Длина: 9,18 м\nМатериал корпуса: стеклопластик",
                "specifications_source_name": "Официальный каталог",
                "specifications_source_url": "https://example.com/axopar",
                "photo": (io.BytesIO(b"test-image-content"), "axopar.webp"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/tuning/boats/{profile_id}"))
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            self.assertEqual(
                profile["specifications"],
                "Длина: 9,18 м\nМатериал корпуса: стеклопластик",
            )
            self.assertTrue(profile["photo_filename"].endswith(".webp"))
            self.assertEqual(
                profile["specifications_source_url"],
                "https://example.com/axopar",
            )
            self.assertTrue(
                os.path.exists(
                    os.path.join(
                        self.upload_directory.name,
                        "tuning_boats",
                        profile["photo_filename"],
                    )
                )
            )

        page = self.client.get(f"/tuning/boats/{profile_id}")
        page_html = page.get_data(as_text=True)
        self.assertIn("<dt>Длина</dt>", page_html)
        self.assertIn("<dd>9,18 м</dd>", page_html)
        self.assertIn("Официальный каталог ↗", page_html)
        self.assertIn("Профиль лодки обновлён.", page_html)

    def test_known_models_are_seeded_without_overwriting_manual_data(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            self.create_order(
                db, "BRP Utopia 205", "Клиент", 1000, "2026-08-24 10:00"
            )
            self.create_order(
                db, "Bayliner 192", "Клиент", 1000, "2026-08-24 11:00"
            )
            self.create_order(
                db, "Неизвестная лодка", "Клиент", 1000, "2026-08-24 12:00"
            )
            application_module._sync_tuning_boat_profiles(db)
            db.execute(
                "UPDATE tuning_boat_profiles SET specifications = 'Ручное значение' "
                "WHERE model_key = 'bayliner 192'"
            )
            application_module._sync_tuning_boat_profiles(db)
            db.commit()

            profiles = {
                row["model_key"]: row
                for row in db.execute("SELECT * FROM tuning_boat_profiles").fetchall()
            }
            self.assertIn("Длина: 6,05 м", profiles["brp utopia 205"]["specifications"])
            self.assertIn(
                "sea-doo.brp.com",
                profiles["brp utopia 205"]["specifications_source_url"],
            )
            self.assertEqual(
                profiles["bayliner 192"]["specifications"], "Ручное значение"
            )
            self.assertEqual(
                profiles["неизвестная лодка"]["specifications"], ""
            )

    def test_profile_name_can_be_renamed_without_splitting_history(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            first_order_id = self.create_order(
                db, "Old Boat 600", "Первый клиент", 1000, "2026-08-25 10:00"
            )
            second_order_id = self.create_order(
                db, "  OLD   BOAT 600  ", "Второй клиент", 2000, "2026-08-26 10:00"
            )
            client_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Владелец лодки', 'old boat 600', '', "
                "'boat-catalog-rename-test', '2026-08-25 10:00')"
            ).lastrowid
            application_module._sync_tuning_boat_profiles(db)
            profile_id = db.execute(
                "SELECT id FROM tuning_boat_profiles WHERE model_key = 'old boat 600'"
            ).fetchone()["id"]
            sheet_id = db.execute(
                "INSERT INTO field_diagnostic_sheets "
                "(boat_profile_id, boat_model, owner_name, owner_phone, "
                "inspection_type, status, created_by_name, started_at) "
                "VALUES (?, 'Old Boat 600', 'Владелец', '', 'water', "
                "'in_progress', 'Мастер', '2026-08-25 10:00')",
                (profile_id,),
            ).lastrowid
            db.commit()

        self.addCleanup(self._delete_rename_fixture, client_id, sheet_id)
        self.login()
        response = self.client.post(
            f"/tuning/boats/{profile_id}/edit",
            data={"model_name": "  New   Boat  650  "},
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/tuning/boats/{profile_id}"))
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            orders = db.execute(
                "SELECT id, boat_model FROM tuning_orders WHERE id IN (?, ?) ORDER BY id",
                (first_order_id, second_order_id),
            ).fetchall()
            client_model = db.execute(
                "SELECT boat_model FROM clients WHERE id = ?", (client_id,)
            ).fetchone()["boat_model"]
            sheet_model = db.execute(
                "SELECT boat_model FROM field_diagnostic_sheets WHERE id = ?",
                (sheet_id,),
            ).fetchone()["boat_model"]

        self.assertEqual(profile["model_name"], "New Boat 650")
        self.assertEqual(profile["model_key"], "new boat 650")
        self.assertEqual([row["boat_model"] for row in orders], ["New Boat 650"] * 2)
        self.assertEqual(client_model, "New Boat 650")
        self.assertEqual(sheet_model, "New Boat 650")

        profile_page = self.client.get(f"/tuning/boats/{profile_id}").get_data(
            as_text=True
        )
        self.assertIn("New Boat 650", profile_page)
        self.assertIn("Название модели и связанные записи обновлены.", profile_page)
        self.assertIn('class="boat-profile-name-toggle"', profile_page)
        self.assertIn('aria-label="Изменить название модели"', profile_page)
        self.assertNotIn("<summary>Изменить название модели</summary>", profile_page)
        self.assertIn(f"№{first_order_id}", profile_page)
        self.assertIn(f"№{second_order_id}", profile_page)
        orders_page = self.client.get("/tuning").get_data(as_text=True)
        self.assertIn("New Boat 650", orders_page)
        self.assertEqual(
            orders_page.count(f'href="/tuning/boats/{profile_id}"'), 2
        )

    @staticmethod
    def _delete_rename_fixture(client_id, sheet_id):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM field_diagnostic_sheets WHERE id = ?", (sheet_id,))
            db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
            db.commit()

    def test_profile_name_rejects_blank_duplicate_and_oversized_values(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            self.create_order(db, "Model Alpha", "Клиент", 1000, "2026-08-25 10:00")
            self.create_order(db, "Model Beta", "Клиент", 1000, "2026-08-26 10:00")
            application_module._sync_tuning_boat_profiles(db)
            profiles = {
                row["model_key"]: row["id"]
                for row in db.execute(
                    "SELECT id, model_key FROM tuning_boat_profiles"
                ).fetchall()
            }
            db.commit()

        self.login()
        for invalid_name, expected_message in (
            ("   ", "Укажите название модели лодки."),
            ("model beta", "Модель с таким названием уже есть в каталоге."),
            ("x" * 201, "Название модели не должно превышать 200 символов."),
        ):
            with self.subTest(invalid_name=invalid_name[:20]):
                response = self.client.post(
                    f"/tuning/boats/{profiles['model alpha']}/edit",
                    data={"model_name": invalid_name},
                    follow_redirects=True,
                )
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn(expected_message, html)
                self.assertIn('class="boat-profile-name-editor" open', html)

        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT model_key, model_name FROM tuning_boat_profiles WHERE id = ?",
                (profiles["model alpha"],),
            ).fetchone()
            order_model = db.execute(
                "SELECT boat_model FROM tuning_orders WHERE boat_model = 'Model Alpha'"
            ).fetchone()["boat_model"]
        self.assertEqual(profile["model_key"], "model alpha")
        self.assertEqual(profile["model_name"], "Model Alpha")
        self.assertEqual(order_model, "Model Alpha")

    def test_profile_rejects_oversized_characteristics_and_wrong_photo_type(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            self.create_order(db, "Silver Hawk", "Клиент", 1000, "2026-08-23 10:00")
            application_module._sync_tuning_boat_profiles(db)
            db.commit()
            profile_id = db.execute(
                "SELECT id FROM tuning_boat_profiles WHERE model_key = 'silver hawk'"
            ).fetchone()["id"]

        self.login()
        too_long = self.client.post(
            f"/tuning/boats/{profile_id}/edit",
            data={"specifications": "x" * 8001},
        )
        wrong_photo = self.client.post(
            f"/tuning/boats/{profile_id}/edit",
            data={
                "specifications": "Не должно сохраниться",
                "photo": (io.BytesIO(b"not-an-image"), "boat.pdf"),
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(too_long.status_code, 302)
        self.assertEqual(wrong_photo.status_code, 302)
        with application_module.app.app_context():
            profile = application_module.get_db().execute(
                "SELECT * FROM tuning_boat_profiles WHERE id = ?", (profile_id,)
            ).fetchone()
            self.assertEqual(profile["specifications"], "")
            self.assertIsNone(profile["photo_filename"])

    def test_catalog_and_profiles_require_admin_login(self):
        catalog = self.client.get("/tuning/boats")
        profile = self.client.get("/tuning/boats/1")
        update = self.client.post("/tuning/boats/1/edit")

        for response in (catalog, profile, update):
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()

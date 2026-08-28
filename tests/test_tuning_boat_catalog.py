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
        self.assertIn("Длина: 9,18 м", page.get_data(as_text=True))
        self.assertIn("Профиль лодки обновлён.", page.get_data(as_text=True))

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

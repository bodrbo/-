import io
import tempfile
import unittest

from support import application_module


class SupplyLockersTests(unittest.TestCase):
    EMPLOYEE_NAME = "Локер Тестовый"
    USERNAME = "locker.tester"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        self.upload_directory = tempfile.TemporaryDirectory()
        self.original_static_folder = application_module.app.static_folder
        application_module.app.static_folder = self.upload_directory.name
        self.addCleanup(self.upload_directory.cleanup)
        self.addCleanup(
            setattr,
            application_module.app,
            "static_folder",
            self.original_static_folder,
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            employee = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES (?, '2026-09-15 09:00', NULL)",
                (self.EMPLOYEE_NAME,),
            )
            account = db.execute(
                "INSERT INTO team_accounts "
                "(employee_id, employee_name, username, password_hash, created_at) "
                "VALUES (?, ?, ?, 'test-hash', '2026-09-15 09:00')",
                (employee.lastrowid, self.EMPLOYEE_NAME, self.USERNAME),
            )
            self.team_account_id = account.lastrowid
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        db.execute("DELETE FROM team_accounts WHERE employee_name = ?", (cls.EMPLOYEE_NAME,))
        db.execute("DELETE FROM employees WHERE name = ?", (cls.EMPLOYEE_NAME,))
        db.execute(
            "DELETE FROM supply_lockers WHERE name != 'Сундук-постамат №1'"
        )
        db.commit()

    def login_as_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def login_as_team(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.team_account_id
            session["team_employee_name"] = self.EMPLOYEE_NAME
            session["team_username"] = self.USERNAME

    def seeded_locker(self):
        with application_module.app.app_context():
            return dict(
                application_module.get_db()
                .execute(
                    "SELECT * FROM supply_lockers WHERE name = 'Сундук-постамат №1'"
                )
                .fetchone()
            )

    def test_bundled_first_locker_is_seeded(self):
        locker = self.seeded_locker()
        self.assertEqual(locker["address"], "Кронштадт, Цитадельское шоссе дом 4")
        self.assertEqual(locker["volume"], "340л")
        self.assertEqual(locker["access_code"], "000")

    def test_admin_list_requires_login_and_renders_seeded_locker(self):
        response = self.client.get("/supply/lockers")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))

        self.login_as_admin()
        response = self.client.get("/supply/lockers")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Сундук-постамат №1".encode(), response.data)
        self.assertIn("Постаматы".encode(), response.data)

    def test_admin_creates_locker(self):
        self.login_as_admin()
        response = self.client.post(
            "/supply/lockers/add",
            data={
                "name": "Сундук-постамат №2",
                "address": "Санкт-Петербург, наб. Обводного канала",
                "volume": "180л",
                "access_code": "1234",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            locker = dict(
                application_module.get_db()
                .execute(
                    "SELECT * FROM supply_lockers WHERE name = 'Сундук-постамат №2'"
                )
                .fetchone()
            )
        self.assertEqual(locker["volume"], "180л")
        self.assertEqual(locker["access_code"], "1234")

    def test_admin_create_without_name_fails(self):
        self.login_as_admin()
        response = self.client.post(
            "/supply/lockers/add", data={"name": "  ", "address": "", "volume": "", "access_code": ""}
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIn("locker_error", session)

    def test_admin_can_edit_all_fields(self):
        self.login_as_admin()
        locker = self.seeded_locker()
        response = self.client.post(
            f"/supply/lockers/{locker['id']}",
            data={
                "name": "Сундук-постамат №1 (переименован)",
                "address": "Новый адрес",
                "volume": "500л",
                "access_code": "999",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            updated = dict(
                application_module.get_db()
                .execute("SELECT * FROM supply_lockers WHERE id = ?", (locker["id"],))
                .fetchone()
            )
        self.assertEqual(updated["name"], "Сундук-постамат №1 (переименован)")
        self.assertEqual(updated["address"], "Новый адрес")
        self.assertEqual(updated["volume"], "500л")
        self.assertEqual(updated["access_code"], "999")
        # restore the seed row's original values for other tests
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE supply_lockers SET name = 'Сундук-постамат №1', "
                "address = 'Кронштадт, Цитадельское шоссе дом 4', volume = '340л', "
                "access_code = '000' WHERE id = ?",
                (locker["id"],),
            )
            db.commit()

    def test_team_lockers_require_team_login_and_hide_edit_form(self):
        response = self.client.get("/team/lockers")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/team/login"))

        self.login_as_team()
        response = self.client.get("/team/lockers")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Сундук-постамат №1".encode(), response.data)

        locker = self.seeded_locker()
        detail = self.client.get(f"/team/lockers/{locker['id']}")
        self.assertEqual(detail.status_code, 200)
        body = detail.get_data(as_text=True)
        self.assertIn("340л", body)
        self.assertIn("000", body)
        self.assertNotIn('name="access_code"', body)
        self.assertNotIn("<form method=\"post\"", body)

    def test_team_cannot_edit_locker_via_admin_route(self):
        self.login_as_team()
        locker = self.seeded_locker()
        response = self.client.post(
            f"/supply/lockers/{locker['id']}",
            data={"name": "Hacked", "address": "", "volume": "", "access_code": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))
        unchanged = self.seeded_locker()
        self.assertEqual(unchanged["name"], "Сундук-постамат №1")

    def test_admin_uploads_photo_and_it_shows_on_both_profiles(self):
        self.login_as_admin()
        locker = self.seeded_locker()
        response = self.client.post(
            f"/supply/lockers/{locker['id']}",
            data={
                "name": locker["name"],
                "address": locker["address"],
                "volume": locker["volume"],
                "access_code": locker["access_code"],
                "photo": (io.BytesIO(b"test-image-content"), "locker.webp"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            updated = dict(
                application_module.get_db()
                .execute("SELECT * FROM supply_lockers WHERE id = ?", (locker["id"],))
                .fetchone()
            )
        self.assertTrue(updated["photo_filename"].endswith(".webp"))

        admin_page = self.client.get(f"/supply/lockers/{locker['id']}")
        self.assertIn(
            f"/static/supply_lockers/{updated['photo_filename']}".encode(),
            admin_page.data,
        )

        self.login_as_team()
        team_page = self.client.get(f"/team/lockers/{locker['id']}")
        self.assertIn(
            f"/static/supply_lockers/{updated['photo_filename']}".encode(),
            team_page.data,
        )
        self.assertNotIn(b'name="photo"', team_page.data)

        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "UPDATE supply_lockers SET photo_filename = NULL WHERE id = ?",
                (locker["id"],),
            )
            db.commit()

    def test_admin_rejects_unsupported_photo_type(self):
        self.login_as_admin()
        locker = self.seeded_locker()
        response = self.client.post(
            f"/supply/lockers/{locker['id']}",
            data={
                "name": locker["name"],
                "address": locker["address"],
                "volume": locker["volume"],
                "access_code": locker["access_code"],
                "photo": (io.BytesIO(b"not-really-a-pdf"), "locker.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIn("locker_error", session)
        unchanged = self.seeded_locker()
        self.assertIsNone(unchanged["photo_filename"])

    def _add_locker(self, name, **overrides):
        self.login_as_admin()
        data = {"address": "", "volume": "", "access_code": ""}
        data.update(overrides)
        response = self.client.post(
            "/supply/lockers/add",
            data={"name": name, **data},
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            return dict(
                application_module.get_db()
                .execute("SELECT * FROM supply_lockers WHERE name = ?", (name,))
                .fetchone()
            )

    def test_bulk_delete_requires_admin_login(self):
        locker = self._add_locker("Постамат для удаления 1")
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.post(
            "/supply/lockers/bulk-delete", data={"locker_id": [str(locker["id"])]}
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/admin/login"))
        with application_module.app.app_context():
            still_there = application_module.get_db().execute(
                "SELECT 1 FROM supply_lockers WHERE id = ?", (locker["id"],)
            ).fetchone()
        self.assertIsNotNone(still_there)

    def test_bulk_delete_without_selection_errors(self):
        self.login_as_admin()
        response = self.client.post("/supply/lockers/bulk-delete", data={})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as session:
            self.assertIn("locker_error", session)

    def test_bulk_delete_removes_unused_lockers(self):
        first = self._add_locker("Постамат для удаления 2")
        second = self._add_locker("Постамат для удаления 3")
        self.login_as_admin()
        response = self.client.post(
            "/supply/lockers/bulk-delete",
            data={"locker_id": [str(first["id"]), str(second["id"])]},
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            remaining = application_module.get_db().execute(
                "SELECT COUNT(*) AS c FROM supply_lockers WHERE id IN (?, ?)",
                (first["id"], second["id"]),
            ).fetchone()["c"]
        self.assertEqual(remaining, 0)
        with self.client.session_transaction() as session:
            notice = session.get("locker_notice")
        self.assertIsNotNone(notice)
        self.assertEqual(notice["type"], "success")
        self.assertIn("Удалено 2", notice["message"])

    def test_bulk_delete_skips_locker_with_return_history(self):
        locker = self._add_locker("Постамат с историей возврата")
        with application_module.app.app_context():
            db = application_module.get_db()
            req = db.execute(
                "INSERT INTO supply_requests (employee_name, status, created_at) "
                "VALUES (?, 'new', '2026-09-16 09:00')",
                (self.EMPLOYEE_NAME,),
            )
            request_id = req.lastrowid
            db.execute(
                "INSERT INTO supply_request_returns "
                "(request_id, locker_id, returned_at, created_at) VALUES (?, ?, ?, ?)",
                (request_id, locker["id"], "2026-09-16 09:00", "2026-09-16 09:00"),
            )
            db.commit()

        def _cleanup_request():
            with application_module.app.app_context():
                cleanup_db = application_module.get_db()
                cleanup_db.execute(
                    "DELETE FROM supply_request_returns WHERE request_id = ?",
                    (request_id,),
                )
                cleanup_db.execute(
                    "DELETE FROM supply_requests WHERE id = ?", (request_id,)
                )
                cleanup_db.commit()

        self.addCleanup(_cleanup_request)

        self.login_as_admin()
        response = self.client.post(
            "/supply/lockers/bulk-delete", data={"locker_id": [str(locker["id"])]}
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            still_there = application_module.get_db().execute(
                "SELECT 1 FROM supply_lockers WHERE id = ?", (locker["id"],)
            ).fetchone()
        self.assertIsNotNone(still_there)
        with self.client.session_transaction() as session:
            notice = session.get("locker_notice")
        self.assertEqual(notice["type"], "error")
        self.assertIn("Пропущено 1", notice["message"])

    def test_bulk_delete_ignores_garbage_ids(self):
        locker = self._add_locker("Постамат для удаления 4")
        self.login_as_admin()
        response = self.client.post(
            "/supply/lockers/bulk-delete",
            data={"locker_id": ["not-a-number", str(locker["id"])]},
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            still_there = application_module.get_db().execute(
                "SELECT 1 FROM supply_lockers WHERE id = ?", (locker["id"],)
            ).fetchone()
        self.assertIsNone(still_there)


if __name__ == "__main__":
    unittest.main()

import unittest
from urllib.parse import parse_qs, urlparse

from support import application_module


class ClientDirectoryPaginationTests(unittest.TestCase):
    TOKEN_PREFIX = "client-pagination-test-"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            self.tuning_ids = self._create_clients(
                db, "Пагинация Тюнинг", "tuning", 25
            )
            self.excursion_ids = self._create_clients(
                db, "Пагинация Экскурсия", "excursion", 25
            )
            self.manager_account_id = db.execute(
                "SELECT ta.id FROM team_accounts ta JOIN employees e "
                "ON e.id = ta.employee_id JOIN employee_positions ep "
                "ON ep.employee_id = e.id "
                "WHERE ep.position = ? ORDER BY ta.id LIMIT 1",
                ("Менеджер по работе с клиентами",),
            ).fetchone()["id"]
            db.commit()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        db.execute(
            "DELETE FROM client_segments WHERE client_id IN "
            "(SELECT id FROM clients WHERE token LIKE ?)",
            (f"{cls.TOKEN_PREFIX}%",),
        )
        db.execute(
            "DELETE FROM clients WHERE token LIKE ?",
            (f"{cls.TOKEN_PREFIX}%",),
        )
        db.commit()

    @classmethod
    def _create_clients(cls, db, name_prefix, segment, count):
        client_ids = []
        for index in range(1, count + 1):
            cursor = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, status, created_at) "
                "VALUES (?, '', ?, ?, 'neutral', '2099-01-01 12:00')",
                (
                    f"{name_prefix} {index:02d}",
                    f"+7999000{index:04d}",
                    f"{cls.TOKEN_PREFIX}{segment}-{index:02d}",
                ),
            )
            client_ids.append(cursor.lastrowid)
            db.execute(
                "INSERT INTO client_segments (client_id, segment, created_at) "
                "VALUES (?, ?, '2099-01-01 12:00')",
                (cursor.lastrowid, segment),
            )
        return client_ids

    def login_as_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def login_as_manager(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.manager_account_id
            session["team_employee_name"] = "Менеджер"
            session["team_username"] = "manager-pagination-test"

    def test_start_page_loads_only_twenty_clients(self):
        self.login_as_admin()

        html = self.client.get("/admin/clients").get_data(as_text=True)

        self.assertEqual(html.count('class="tuning-client-name"'), 20)
        self.assertIn("Пагинация Тюнинг 25", html)
        self.assertIn("Пагинация Тюнинг 06", html)
        self.assertNotIn("Пагинация Тюнинг 05", html)
        self.assertIn("1–20 из", html)
        self.assertIn('aria-label="Следующая страница"', html)

    def test_search_is_case_insensitive_and_paginates_all_matches(self):
        self.login_as_admin()
        query = "пАГИНАЦИЯ тЮНИНГ"

        first_page = self.client.get(
            "/admin/clients", query_string={"q": query}
        ).get_data(as_text=True)
        second_page = self.client.get(
            "/admin/clients", query_string={"q": query, "page": 2}
        ).get_data(as_text=True)

        self.assertEqual(first_page.count('class="tuning-client-name"'), 20)
        self.assertIn("1–20 из 25", first_page)
        self.assertIn("Пагинация Тюнинг 25", first_page)
        self.assertNotIn("Пагинация Тюнинг 05", first_page)
        self.assertEqual(second_page.count('class="tuning-client-name"'), 5)
        self.assertIn("21–25 из 25", second_page)
        self.assertIn("Пагинация Тюнинг 05", second_page)
        self.assertNotIn("Пагинация Тюнинг 25", second_page)
        self.assertIn('name="q" value="пАГИНАЦИЯ тЮНИНГ"', second_page)

    def test_invalid_and_out_of_range_pages_are_clamped(self):
        self.login_as_admin()
        query = "Пагинация Тюнинг"

        invalid_page = self.client.get(
            "/admin/clients", query_string={"q": query, "page": "wrong"}
        ).get_data(as_text=True)
        high_page = self.client.get(
            "/admin/clients", query_string={"q": query, "page": 999}
        ).get_data(as_text=True)

        self.assertIn("1–20 из 25", invalid_page)
        self.assertIn("21–25 из 25", high_page)
        self.assertIn('<span class="current" aria-current="page">2</span>', high_page)

    def test_manager_pagination_stays_in_excursion_directory(self):
        self.login_as_manager()
        query = "Пагинация Экскурсия"

        response = self.client.get(
            "/admin/clients",
            query_string={"section": "tuning", "q": query, "page": 2},
        )
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(html.count('class="tuning-client-name"'), 5)
        self.assertIn("21–25 из 25", html)
        self.assertIn("Пагинация Экскурсия 05", html)
        self.assertNotIn("Пагинация Тюнинг", html)
        self.assertNotIn("Клиенты тюнинга", html)

    def test_status_change_keeps_current_page_and_search(self):
        self.login_as_admin()
        response = self.client.post(
            f"/admin/clients/{self.excursion_ids[0]}/status",
            data={
                "status": "satisfied",
                "section": "excursion",
                "page": "2",
                "q": "Пагинация Экскурсия",
            },
        )

        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.headers["Location"]).query)
        self.assertEqual(query["section"], ["excursion"])
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["q"], ["Пагинация Экскурсия"])


if __name__ == "__main__":
    unittest.main()

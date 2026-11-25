import json
import re
import unittest

from werkzeug.security import generate_password_hash

from support import application_module as application


class NavigationPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.client = application.app.test_client()
        self.ids = []
        with application.app.app_context():
            db = application.get_db()
            for name in ("navigation-one", "navigation-two"):
                self.ids.append(db.execute(
                    "INSERT INTO admin_accounts (admin_name, username, password_hash, created_at) VALUES (?, ?, ?, '2026-11-25')",
                    (name, name, generate_password_hash("test-password", method="pbkdf2:sha256")),
                ).lastrowid)
            db.commit()
        self.login(self.ids[0])

    def tearDown(self):
        with application.app.app_context():
            db = application.get_db()
            for account_id in self.ids:
                db.execute("DELETE FROM navigation_preferences WHERE owner_type='admin' AND owner_id=?", (account_id,))
                db.execute("DELETE FROM admin_accounts WHERE id=?", (account_id,))
            db.commit()

    def login(self, account_id):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = account_id

    def config(self, path="/tuning"):
        response = self.client.get(path, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        match = re.search(r'<script id="navigation-preferences-data" type="application/json">(.*?)</script>', response.get_data(as_text=True), re.S)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def save(self, token):
        return self.client.post("/api/me/start-page", json={"token": token})

    def test_independent_defaults_persist_on_home_and_login(self):
        config = self.config()
        with application.app.test_request_context():
            board = application.url_for("tuning_projects_board")
            schedule = application.url_for("tuning_schedule.index")
        self.assertEqual(self.save(config["links"][board]).status_code, 200)
        self.assertEqual(self.client.get("/").headers["Location"], board)
        self.assertEqual(self.client.get("/tuning").status_code, 200)
        self.login(self.ids[1])
        self.assertEqual(self.client.get("/").status_code, 200)
        config = self.config(schedule)
        self.assertEqual(self.save(config["links"][schedule]).status_code, 200)
        self.assertEqual(self.client.get("/").headers["Location"], schedule)
        self.login(self.ids[0])
        self.assertEqual(self.client.get("/admin/login").headers["Location"], board)
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.post("/admin/login", data={"username": "navigation-one", "password": "test-password"})
        self.assertEqual(response.headers["Location"], board)

    def test_reset_and_cross_account_tokens(self):
        config = self.config()
        token = next(iter(config["links"].values()))
        self.assertEqual(self.save(token).status_code, 200)
        self.login(self.ids[1])
        self.assertEqual(self.save(token).status_code, 403)
        self.login(self.ids[0])
        self.assertEqual(self.save(config["reset"]).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_invalid_unsigned_and_anonymous_requests(self):
        self.assertEqual(self.save("https://evil.example").status_code, 400)
        self.assertEqual(self.client.post("/api/me/start-page", json=[]).status_code, 400)
        token = next(iter(self.config()["links"].values()))
        self.assertEqual(self.client.post("/api/me/start-page", json={"token": token}, headers={"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.save(token).status_code, 401)


if __name__ == "__main__":
    unittest.main()

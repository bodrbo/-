import os
import tempfile
import unittest

from werkzeug.security import generate_password_hash

from support import application_module


class PersistentLoginTests(unittest.TestCase):
    """A login lasts: the cookie is persistent (survives closing the browser)
    and is renewed on every visit, and the signing key survives restarts."""

    USERNAME = "persistent-login-admin"
    PASSWORD = "correct horse battery"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM admin_accounts WHERE username = ?", (self.USERNAME,))
            db.execute(
                "INSERT INTO admin_accounts (admin_name, username, password_hash, created_at) "
                "VALUES ('Тест', ?, ?, '2026-10-01 10:00')",
                (self.USERNAME, generate_password_hash(self.PASSWORD, method="pbkdf2:sha256")),
            )
            db.commit()
        self.addCleanup(self.cleanup)
        self.client = application_module.app.test_client()

    def cleanup(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM admin_accounts WHERE username = ?", (self.USERNAME,))
            db.commit()

    def login(self):
        return self.client.post("/admin/login", data={"username": self.USERNAME, "password": self.PASSWORD})

    @staticmethod
    def session_cookie_lines(response):
        return [h for h in response.headers.getlist("Set-Cookie") if h.startswith("session=")]

    def test_the_login_cookie_is_persistent_and_hardened(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        (cookie,) = self.session_cookie_lines(response)
        self.assertIn("Expires=", cookie)          # not a browser-session cookie
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)

    def test_the_lifetime_is_30_days_by_default(self):
        self.assertEqual(application_module.SESSION_LIFETIME_DAYS, 30)
        self.assertEqual(application_module.app.permanent_session_lifetime.days, 30)

    def test_every_visit_renews_the_cookie(self):
        self.login()
        response = self.client.get("/trips")
        self.assertTrue(self.session_cookie_lines(response))
        self.assertIn("Expires=", self.session_cookie_lines(response)[0])

    def test_a_session_made_without_the_login_form_becomes_persistent_on_the_next_visit(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        response = self.client.get("/trips")
        lines = self.session_cookie_lines(response)
        self.assertTrue(lines and "Expires=" in lines[0])

    def test_a_visitor_without_a_login_gets_no_session_cookie(self):
        response = application_module.app.test_client().get("/admin/login")
        self.assertEqual(self.session_cookie_lines(response), [])

    def test_logging_out_ends_the_session(self):
        self.login()
        self.assertEqual(self.client.get("/trips").status_code, 200)
        self.client.post("/admin/logout")
        response = self.client.get("/trips")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.headers["Location"])


class SecretKeyTests(unittest.TestCase):
    def test_the_environment_key_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".secret_key")
            self.assertEqual(application_module._resolve_secret_key("from-env", path), "from-env")
            self.assertFalse(os.path.exists(path))

    def test_without_an_environment_key_one_is_generated_once_and_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".secret_key")
            first = application_module._resolve_secret_key(None, path)
            second = application_module._resolve_secret_key("", path)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)   # private file

    def test_an_unwritable_location_still_gives_a_working_key(self):
        key = application_module._resolve_secret_key(None, "/nonexistent-folder/.secret_key")
        self.assertGreaterEqual(len(key), 32)

    def test_a_too_short_stored_key_is_replaced_not_trusted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, ".secret_key")
            with open(path, "w") as handle:
                handle.write("short")
            key = application_module._resolve_secret_key(None, path)
            self.assertGreaterEqual(len(key), 32)


if __name__ == "__main__":
    unittest.main()

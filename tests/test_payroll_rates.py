import unittest

from support import application_module
from modules.payroll_rates import repository as payroll_rates_repository
from modules.payroll_rates import services as payroll_rates_services
from modules.payroll_rates.constants import DEFAULT_EXCURSION_ROLE_RATES, EXCURSION_ROLES
from modules.payroll_rates.schema import init_schema


class PayrollRatesTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        with application_module.app.app_context():
            db = application_module.get_db()
            # Tests mutate rates - reset every role back to its default
            # (0 for "guide", the deliberate gap under test) so tests
            # don't leak into each other regardless of order.
            for role in EXCURSION_ROLES:
                db.execute(
                    "UPDATE excursion_role_rates SET rate = ? WHERE role = ?",
                    (DEFAULT_EXCURSION_ROLE_RATES.get(role, 0), role),
                )
            db.commit()
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def test_seeding_matches_defaults_with_no_guide_rate(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            rows = payroll_rates_repository.list_excursion_role_rates(db)
        seeded = {row["role"]: row["rate"] for row in rows}
        self.assertEqual(set(seeded), set(EXCURSION_ROLES))
        self.assertEqual(seeded["captain"], DEFAULT_EXCURSION_ROLE_RATES["captain"])
        self.assertEqual(seeded["guide_captain"], DEFAULT_EXCURSION_ROLE_RATES["guide_captain"])
        self.assertEqual(seeded["guide"], 0)

    def test_reinit_schema_does_not_duplicate_or_reset_rows(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            payroll_rates_services.set_excursion_role_rate(db, "captain", "1234")
            init_schema(db)
            init_schema(db)
            rows = payroll_rates_repository.list_excursion_role_rates(db)
            rate = payroll_rates_repository.get_excursion_role_rate(db, "captain")
        self.assertEqual(len(rows), len(EXCURSION_ROLES))
        self.assertEqual(rate, 1234.0)

    def test_set_excursion_role_rate_validates_and_persists(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            ok, message = payroll_rates_services.set_excursion_role_rate(db, "guide", "950")
            self.assertTrue(ok, message)
            self.assertEqual(
                payroll_rates_repository.get_excursion_role_rate(db, "guide"), 950.0,
            )

            ok, message = payroll_rates_services.set_excursion_role_rate(db, "guide", "1000")
            self.assertTrue(ok, message)
            self.assertEqual(
                payroll_rates_repository.get_excursion_role_rate(db, "guide"), 1000.0,
            )
            count = db.execute(
                "SELECT COUNT(*) AS c FROM excursion_role_rates WHERE role = 'guide'"
            ).fetchone()["c"]
            self.assertEqual(count, 1)

            ok, message = payroll_rates_services.set_excursion_role_rate(db, "not-a-role", "500")
            self.assertFalse(ok)

            ok, message = payroll_rates_services.set_excursion_role_rate(db, "captain", "not-a-number")
            self.assertFalse(ok)

            ok, message = payroll_rates_services.set_excursion_role_rate(db, "captain", "-5")
            self.assertFalse(ok)

    def test_excursion_rates_page_requires_admin_and_updates(self):
        anon = application_module.app.test_client()
        resp = anon.get("/payroll/rates/excursions")
        self.assertEqual(resp.status_code, 302)

        resp = self.client.get("/payroll/rates")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/payroll/rates/excursions", resp.headers["Location"])

        resp = self.client.get("/payroll/rates/excursions")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Капитан".encode(), resp.data)

        resp = self.client.post(
            "/payroll/rates/excursions",
            data={"role": "guide", "rate": "1234"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        with application_module.app.app_context():
            db = application_module.get_db()
            rate = payroll_rates_repository.get_excursion_role_rate(db, "guide")
        self.assertEqual(rate, 1234.0)

    def test_tuning_rates_placeholder_page_requires_admin(self):
        anon = application_module.app.test_client()
        resp = anon.get("/payroll/rates/tuning")
        self.assertEqual(resp.status_code, 302)

        resp = self.client.get("/payroll/rates/tuning")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Ставки тюнинга".encode(), resp.data)

    def test_demo_without_tuning_hides_and_blocks_tuning_rates(self):
        with self.client.session_transaction() as demo_session:
            demo_session.clear()
            demo_session["demo_tenant_id"] = 903
            demo_session["demo_tenant_name"] = "Демо морских прогулок"
            demo_session["demo_tenant_db_path"] = application_module.DB_PATH
            demo_session["demo_tenant_modules"] = "excursions"

        resp = self.client.get("/payroll/rates/excursions")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Ставки экскурсий".encode(), resp.data)
        self.assertNotIn('href="/payroll/rates/tuning"'.encode(), resp.data)
        self.assertEqual(self.client.get("/payroll/rates/tuning").status_code, 404)

    def test_demo_with_tuning_keeps_tuning_rates(self):
        with self.client.session_transaction() as demo_session:
            demo_session.clear()
            demo_session["demo_tenant_id"] = 904
            demo_session["demo_tenant_name"] = "Демо тюнинга"
            demo_session["demo_tenant_db_path"] = application_module.DB_PATH
            demo_session["demo_tenant_modules"] = "tuning"

        resp = self.client.get("/payroll/rates/excursions")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('href="/payroll/rates/tuning"'.encode(), resp.data)
        self.assertEqual(self.client.get("/payroll/rates/tuning").status_code, 200)


if __name__ == "__main__":
    unittest.main()

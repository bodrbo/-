import unittest

from support import application_module
from modules.ai_assistant.data_catalog import catalog_for_user
from modules.ai_assistant.data_gateway import DataGatewayError, run_read_only


class AIDataCatalogTests(unittest.TestCase):
    def user(self, owner_type="employee", positions=None):
        return {
            "owner_type": owner_type,
            "owner_id": 1,
            "name": "Пользователь каталога",
            "positions": positions or [],
        }

    def dataset_ids(self, user):
        return {item["id"] for item in catalog_for_user(user)["datasets"]}

    def test_admin_catalog_contains_all_business_datasets_without_secrets(self):
        catalog = catalog_for_user(self.user(owner_type="admin"))
        self.assertEqual(
            self.dataset_ids(self.user(owner_type="admin")),
            {
                "schedule", "tuning_orders", "excursion_clients", "tuning_clients",
                "payroll", "tasks", "fleet", "employees",
            },
        )
        serialized = str(catalog).casefold()
        self.assertNotIn("password", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("telegram_chat_id", serialized)
        self.assertEqual(catalog["safety"]["raw_sql"], "not_available")
        self.assertTrue(all(not item["write_access"] for item in catalog["datasets"]))
        employees = catalog_for_user(
            self.user(owner_type="admin"), "employees"
        )["datasets"][0]
        self.assertEqual(employees["personal_data"], "employee_names_admin_only")

    def test_catalog_is_filtered_and_scoped_by_role(self):
        employee = self.user()
        manager = self.user(positions=["Менеджер по работе с клиентами"])
        captain = self.user(positions=["Гид-капитан"])

        self.assertEqual(self.dataset_ids(employee), {"payroll", "tasks"})
        self.assertEqual(
            self.dataset_ids(manager),
            {"schedule", "excursion_clients", "payroll", "tasks"},
        )
        self.assertEqual(self.dataset_ids(captain), {"payroll", "tasks", "fleet"})
        payroll = catalog_for_user(employee, "payroll")["datasets"][0]
        self.assertEqual(payroll["access_scope"], "own_only")
        self.assertNotIn("employee_name", payroll["filters"])
        self.assertNotIn("employee", {item["id"] for item in payroll["dimensions"]})

    def test_inaccessible_catalog_dataset_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "недоступен"):
            catalog_for_user(self.user(), "tuning_orders")


class AIReadOnlyGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        application_module.init_db()

    def test_gateway_opens_separate_read_only_connection(self):
        with application_module.app.app_context():
            source = application_module.get_db()

            def inspect(connection):
                return {
                    "separate": connection is not source,
                    "value": connection.execute("SELECT 41 + 1 AS value").fetchone()["value"],
                }

            result = run_read_only(source, inspect)
        self.assertTrue(result["separate"])
        self.assertEqual(result["value"], 42)

    def test_gateway_denies_every_write_even_inside_internal_operation(self):
        with application_module.app.app_context():
            source = application_module.get_db()
            with self.assertRaisesRegex(DataGatewayError, "не выполнен"):
                run_read_only(
                    source,
                    lambda connection: connection.execute(
                        "CREATE TABLE ai_forbidden_write (id INTEGER)"
                    ).fetchall(),
                )
            exists = source.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'ai_forbidden_write'"
            ).fetchone()
        self.assertIsNone(exists)

    def test_gateway_rejects_oversized_result(self):
        with application_module.app.app_context():
            source = application_module.get_db()
            with self.assertRaisesRegex(DataGatewayError, "слишком большой"):
                run_read_only(source, lambda _connection: "x" * 120001)


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

from support import application_module


class TransactionCategoryRuleTests(unittest.TestCase):
    OPERATION_PREFIX = "transaction-rule-test-"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        db.execute(
            "DELETE FROM bank_transactions WHERE operation_id LIKE ?",
            (f"{cls.OPERATION_PREFIX}%",),
        )
        db.execute(
            "DELETE FROM transaction_category_rules "
            "WHERE phrase LIKE 'transaction-rule-test-%'"
        )
        db.commit()

    def _insert_transaction(self, suffix, purpose, category="Категория банка"):
        with application_module.app.app_context():
            db = application_module.get_db()
            cur = db.execute(
                "INSERT INTO bank_transactions "
                "(operation_id, account_number, operation_date, amount, direction, "
                "purpose, category, source_category, created_at, source) "
                "VALUES (?, 'test-account', '2026-09-08', 1000, 'in', ?, ?, ?, "
                "'2026-09-08 12:00', 'tbank')",
                (f"{self.OPERATION_PREFIX}{suffix}", purpose, category, category),
            )
            db.commit()
            return cur.lastrowid

    def test_new_rule_applies_to_existing_transactions_case_insensitively(self):
        transaction_id = self._insert_transaction(
            "existing", "Оплата TRANSACTION-RULE-TEST-Договор №15"
        )

        response = self.client.post(
            "/analytics/transaction-category-rules",
            data={
                "phrase": "transaction-rule-test-договор",
                "category": "Выручка по договорам",
            },
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT category, source_category, category_rule_id "
                "FROM bank_transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            self.assertEqual(row["category"], "Выручка по договорам")
            self.assertEqual(row["source_category"], "Категория банка")
            self.assertIsNotNone(row["category_rule_id"])

    def test_rule_applies_during_tbank_import(self):
        self.client.post(
            "/analytics/transaction-category-rules",
            data={
                "phrase": "transaction-rule-test-эквайринг",
                "category": "Эквайринг",
            },
        )
        operation = {
            "operationId": f"{self.OPERATION_PREFIX}imported",
            "dateTime": "2026-09-08T12:00:00Z",
            "amount": 2500,
            "typeOfOperation": "credit",
            "counterpartyName": "Тестовый клиент",
            "paymentPurpose": "Платёж transaction-rule-test-ЭКВАЙРИНГ",
            "category": "Входящий перевод",
            "status": "completed",
        }

        with patch.object(
            application_module, "tbank_statement_configured", return_value=True
        ), patch.object(
            application_module, "_tbank_fetch_operations", return_value=[operation]
        ), patch.object(
            application_module, "TBANK_ACCOUNT_NUMBER", "test-account"
        ):
            response = self.client.post(
                "/analytics/fetch",
                data={"start_date": "2026-09-08", "end_date": "2026-09-08"},
            )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT category, source_category, category_rule_id "
                "FROM bank_transactions WHERE operation_id = ?",
                (f"{self.OPERATION_PREFIX}imported",),
            ).fetchone()
            self.assertEqual(row["category"], "Эквайринг")
            self.assertEqual(row["source_category"], "Входящий перевод")
            self.assertIsNotNone(row["category_rule_id"])

    def test_deleting_rule_restores_bank_category(self):
        transaction_id = self._insert_transaction(
            "restore", "transaction-rule-test-возврат клиенту"
        )
        self.client.post(
            "/analytics/transaction-category-rules",
            data={"phrase": "transaction-rule-test-возврат", "category": "Возвраты"},
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            rule_id = db.execute(
                "SELECT category_rule_id FROM bank_transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()["category_rule_id"]

        response = self.client.post(
            f"/analytics/transaction-category-rules/{rule_id}/delete"
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            row = application_module.get_db().execute(
                "SELECT category, category_rule_id FROM bank_transactions WHERE id = ?",
                (transaction_id,),
            ).fetchone()
            self.assertEqual(row["category"], "Категория банка")
            self.assertIsNone(row["category_rule_id"])

    def test_longer_matching_phrase_has_priority(self):
        transaction_id = self._insert_transaction(
            "priority", "transaction-rule-test-индивидуальная экскурсия"
        )
        self.client.post(
            "/analytics/transaction-category-rules",
            data={
                "phrase": "transaction-rule-test-индивидуальная",
                "category": "Экскурсии",
            },
        )
        self.client.post(
            "/analytics/transaction-category-rules",
            data={
                "phrase": "transaction-rule-test-индивидуальная экскурсия",
                "category": "Индивидуальные экскурсии",
            },
        )

        with application_module.app.app_context():
            category = application_module.get_db().execute(
                "SELECT category FROM bank_transactions WHERE id = ?", (transaction_id,)
            ).fetchone()["category"]
        self.assertEqual(category, "Индивидуальные экскурсии")


if __name__ == "__main__":
    unittest.main()

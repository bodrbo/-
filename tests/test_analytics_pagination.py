import re
import unittest

from support import application_module


class AnalyticsTransactionPaginationTests(unittest.TestCase):
    OPERATION_PREFIX = "analytics-pagination-test-"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear_test_data(db)
            for index in range(1, 46):
                operation_date = (
                    "2026-08-15T12:00:00Z" if index > 20
                    else "2026-07-15T12:00:00Z"
                )
                db.execute(
                    "INSERT INTO bank_transactions "
                    "(operation_id, account_number, operation_date, amount, direction, "
                    "counterparty_name, purpose, created_at, source) "
                    "VALUES (?, 'test-account', ?, ?, 'in', ?, ?, "
                    "'2026-09-07 12:00', 'tbank')",
                    (
                        f"{self.OPERATION_PREFIX}{index:02d}",
                        operation_date,
                        index * 100,
                        f"Транзакция теста {index:02d}",
                        f"Назначение теста {index:02d}",
                    ),
                )
            db.commit()
        self.login()

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        db.execute(
            "DELETE FROM transaction_splits WHERE transaction_id IN "
            "(SELECT id FROM bank_transactions WHERE operation_id LIKE ?)",
            (f"{cls.OPERATION_PREFIX}%",),
        )
        db.execute(
            "DELETE FROM bank_transactions WHERE operation_id LIKE ?",
            (f"{cls.OPERATION_PREFIX}%",),
        )
        db.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор теста"

    def transaction_rows(self, html):
        body = re.search(r"<tbody>(.*?)</tbody>", html, re.DOTALL)
        self.assertIsNotNone(body)
        return body.group(1).count("<tr>")

    def test_analytics_lists_twenty_transactions_per_page(self):
        first_page = self.client.get("/analytics").get_data(as_text=True)
        second_page = self.client.get(
            "/analytics", query_string={"page": 2}
        ).get_data(as_text=True)
        third_page = self.client.get(
            "/analytics", query_string={"page": 3}
        ).get_data(as_text=True)

        self.assertEqual(self.transaction_rows(first_page), 20)
        self.assertEqual(self.transaction_rows(second_page), 20)
        self.assertEqual(self.transaction_rows(third_page), 5)
        self.assertIn("1–20 из 45", first_page)
        self.assertIn("21–40 из 45", second_page)
        self.assertIn("41–45 из 45", third_page)
        self.assertIn("Транзакция теста 45", first_page)
        self.assertNotIn("Транзакция теста 25", first_page)
        self.assertIn("Транзакция теста 25", second_page)
        self.assertNotIn("Транзакция теста 25", third_page)
        self.assertIn('aria-label="Следующая страница"', first_page)
        self.assertIn('aria-current="page">2</span>', second_page)

    def test_date_filter_is_preserved_across_transaction_pages(self):
        query = {"start": "2026-08-01", "end": "2026-08-31"}
        first_page = self.client.get(
            "/analytics", query_string=query
        ).get_data(as_text=True)
        second_page = self.client.get(
            "/analytics", query_string={**query, "page": 2}
        ).get_data(as_text=True)

        self.assertEqual(self.transaction_rows(first_page), 20)
        self.assertEqual(self.transaction_rows(second_page), 5)
        self.assertIn("1–20 из 25", first_page)
        self.assertIn("21–25 из 25", second_page)
        self.assertIn("за выбранный период", first_page)
        self.assertIn("start=2026-08-01&amp;end=2026-08-31&amp;page=2", first_page)
        self.assertIn(
            'name="next" value="/analytics?start=2026-08-01&amp;end=2026-08-31&amp;page=2"',
            second_page,
        )

    def test_fragment_and_out_of_range_page_use_last_available_page(self):
        response = self.client.get(
            "/analytics/transactions-fragment", query_string={"page": 999}
        )
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transaction_rows(html), 5)
        self.assertIn("41–45 из 45", html)
        self.assertIn('aria-current="page">3</span>', html)

    def test_search_finds_purpose_across_pages_case_insensitively(self):
        response = self.client.get(
            "/analytics", query_string={"q": "нАзНаЧеНиЕ ТеСтА 03"}
        )
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.transaction_rows(html), 1)
        self.assertIn('value="Назначение теста 03"', html)
        self.assertNotIn('value="Назначение теста 45"', html)
        self.assertIn("1–1 из 1", html)
        self.assertIn('value="нАзНаЧеНиЕ ТеСтА 03"', html)

    def test_search_is_preserved_across_pages_and_row_actions(self):
        query = {"q": "назначение теста", "page": 2}
        html = self.client.get("/analytics", query_string=query).get_data(as_text=True)

        self.assertEqual(self.transaction_rows(html), 20)
        self.assertIn('q=%D0%BD%D0%B0%D0%B7%D0%BD%D0%B0%D1%87%D0%B5%D0%BD%D0%B8%D0%B5+', html)
        self.assertIn("page=2", html)


if __name__ == "__main__":
    unittest.main()

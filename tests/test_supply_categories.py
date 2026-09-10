import unittest

from support import application_module


class SupplyCategoryIntegrationTests(unittest.TestCase):
    CATEGORY_NAME = "Тестовые комплектующие"
    PRODUCT_NAME = "Тестовый винт категории"
    PRODUCT_SKU = "category-test-propeller"
    SECOND_PRODUCT_SKU = "category-test-propeller-2"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"

    def tearDown(self):
        with application_module.app.app_context():
            self._clear_test_data(application_module.get_db())

    @classmethod
    def _clear_test_data(cls, db):
        product_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM supply_products WHERE sku IN (?, ?)",
                (cls.PRODUCT_SKU, cls.SECOND_PRODUCT_SKU),
            ).fetchall()
        ]
        for product_id in product_ids:
            db.execute("DELETE FROM supply_stock WHERE product_id = ?", (product_id,))
            db.execute("DELETE FROM supply_receipts WHERE product_id = ?", (product_id,))
            db.execute("DELETE FROM supply_writeoffs WHERE product_id = ?", (product_id,))
            db.execute("DELETE FROM supply_products WHERE id = ?", (product_id,))
        db.execute(
            "DELETE FROM supply_categories WHERE CASEFOLD(name) = ?",
            (cls.CATEGORY_NAME.casefold(),),
        )
        db.commit()

    def _create_category(self, name=None):
        normalized_name = " ".join((name or self.CATEGORY_NAME).split())
        response = self.client.post(
            "/supply/catalog/categories/add",
            data={"name": name or self.CATEGORY_NAME},
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            category = application_module.get_db().execute(
                "SELECT * FROM supply_categories WHERE CASEFOLD(name) = ?",
                (normalized_name.casefold(),),
            ).fetchone()
        self.assertIsNotNone(category)
        return category["id"]

    def _add_product(self, category_id, sku=None, name=None):
        return self.client.post(
            "/supply/catalog/add",
            data={
                "name": name or self.PRODUCT_NAME,
                "sku": sku or self.PRODUCT_SKU,
                "supplier": "Тестовый поставщик",
                "description": "Тест категории",
                "category_id": str(category_id) if category_id is not None else "",
                "cost_price": "100",
                "cost_unit": "piece",
                "sale_price": "150",
                "min_stock": "",
            },
        )

    def _edit_product(self, product_id, category_id):
        return self.client.post(
            f"/supply/catalog/{product_id}/edit",
            data={
                "name": self.PRODUCT_NAME,
                "sku": self.PRODUCT_SKU,
                "supplier": "Тестовый поставщик",
                "description": "Тест категории",
                "category_id": str(category_id) if category_id is not None else "",
                "cost_price": "100",
                "cost_unit": "piece",
                "sale_price": "150",
            },
        )

    def test_schema_migration_adds_category_reference(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            columns = {
                row["name"] for row in db.execute(
                    "PRAGMA table_info(supply_products)"
                ).fetchall()
            }
            category_table = db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'supply_categories'"
            ).fetchone()
        self.assertIn("category_id", columns)
        self.assertIsNotNone(category_table)

    def test_admin_creates_unique_category_case_insensitively(self):
        self._create_category("  Тестовые   комплектующие  ")

        duplicate = self.client.post(
            "/supply/catalog/categories/add",
            data={"name": "тестовые комплектующие"},
            follow_redirects=True,
        )

        self.assertEqual(duplicate.status_code, 200)
        self.assertIn(
            "Категория с таким названием уже существует",
            duplicate.get_data(as_text=True),
        )
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) AS count FROM supply_categories "
                "WHERE CASEFOLD(name) = ?",
                (self.CATEGORY_NAME.casefold(),),
            ).fetchone()["count"]
        self.assertEqual(count, 1)

    def test_product_category_can_be_assigned_and_removed(self):
        category_id = self._create_category()
        response = self._add_product(category_id)
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            product = application_module.get_db().execute(
                "SELECT * FROM supply_products WHERE sku = ?",
                (self.PRODUCT_SKU,),
            ).fetchone()
        self.assertEqual(product["category_id"], category_id)

        catalog_html = self.client.get("/supply/catalog").get_data(as_text=True)
        self.assertIn(self.CATEGORY_NAME, catalog_html)
        self.assertIn("supply-category-badge", catalog_html)
        product_html = self.client.get(
            f"/supply/catalog/{product['id']}"
        ).get_data(as_text=True)
        self.assertIn(
            f'<option value="{category_id}" selected>{self.CATEGORY_NAME}</option>',
            product_html,
        )

        edit_response = self._edit_product(product["id"], None)
        self.assertEqual(edit_response.status_code, 302)
        with application_module.app.app_context():
            category_after = application_module.get_db().execute(
                "SELECT category_id FROM supply_products WHERE id = ?",
                (product["id"],),
            ).fetchone()["category_id"]
        self.assertIsNone(category_after)

    def test_used_category_cannot_be_deleted_until_product_is_unassigned(self):
        category_id = self._create_category()
        self._add_product(category_id)
        with application_module.app.app_context():
            product_id = application_module.get_db().execute(
                "SELECT id FROM supply_products WHERE sku = ?",
                (self.PRODUCT_SKU,),
            ).fetchone()["id"]

        blocked = self.client.post(
            f"/supply/catalog/categories/{category_id}/delete",
            follow_redirects=True,
        )
        self.assertIn("к ней привязаны товары", blocked.get_data(as_text=True))
        with application_module.app.app_context():
            category = application_module.get_db().execute(
                "SELECT id FROM supply_categories WHERE id = ?", (category_id,)
            ).fetchone()
        self.assertIsNotNone(category)

        self._edit_product(product_id, None)
        deleted = self.client.post(
            f"/supply/catalog/categories/{category_id}/delete",
            follow_redirects=True,
        )
        self.assertIn("удалена", deleted.get_data(as_text=True))
        with application_module.app.app_context():
            category = application_module.get_db().execute(
                "SELECT id FROM supply_categories WHERE id = ?", (category_id,)
            ).fetchone()
        self.assertIsNone(category)

    def test_forged_category_is_rejected_for_new_product(self):
        response = self._add_product(99999999)
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/supply/catalog").get_data(as_text=True)
        self.assertIn("Выберите существующую категорию", page)
        with application_module.app.app_context():
            product = application_module.get_db().execute(
                "SELECT id FROM supply_products WHERE sku = ?",
                (self.PRODUCT_SKU,),
            ).fetchone()
        self.assertIsNone(product)

    def test_category_can_be_assigned_and_removed_in_bulk(self):
        category_id = self._create_category()
        self._add_product(None)
        self._add_product(
            None,
            sku=self.SECOND_PRODUCT_SKU,
            name="Второй тестовый товар",
        )
        with application_module.app.app_context():
            rows = application_module.get_db().execute(
                "SELECT id FROM supply_products WHERE sku IN (?, ?) ORDER BY id",
                (self.PRODUCT_SKU, self.SECOND_PRODUCT_SKU),
            ).fetchall()
            product_ids = [row["id"] for row in rows]

        assigned = self.client.post(
            "/supply/catalog/categories/bulk-assign",
            data={
                "category_id": str(category_id),
                "product_id": [str(product_id) for product_id in product_ids],
            },
            follow_redirects=True,
        )
        self.assertEqual(assigned.status_code, 200)
        self.assertIn("назначена", assigned.get_data(as_text=True))
        with application_module.app.app_context():
            category_ids = {
                row["category_id"] for row in application_module.get_db().execute(
                    "SELECT category_id FROM supply_products WHERE sku IN (?, ?)",
                    (self.PRODUCT_SKU, self.SECOND_PRODUCT_SKU),
                ).fetchall()
            }
        self.assertEqual(category_ids, {category_id})

        removed = self.client.post(
            "/supply/catalog/categories/bulk-assign",
            data={
                "category_id": "__none__",
                "product_id": [str(product_id) for product_id in product_ids],
            },
            follow_redirects=True,
        )
        self.assertIn("Категория снята", removed.get_data(as_text=True))
        with application_module.app.app_context():
            category_ids = {
                row["category_id"] for row in application_module.get_db().execute(
                    "SELECT category_id FROM supply_products WHERE sku IN (?, ?)",
                    (self.PRODUCT_SKU, self.SECOND_PRODUCT_SKU),
                ).fetchall()
            }
        self.assertEqual(category_ids, {None})

    def test_bulk_assignment_rejects_missing_products_and_forged_category(self):
        self._add_product(None)
        with application_module.app.app_context():
            product_id = application_module.get_db().execute(
                "SELECT id FROM supply_products WHERE sku = ?",
                (self.PRODUCT_SKU,),
            ).fetchone()["id"]

        forged_category = self.client.post(
            "/supply/catalog/categories/bulk-assign",
            data={"category_id": "99999999", "product_id": str(product_id)},
            follow_redirects=True,
        )
        self.assertIn(
            "Выберите существующую категорию",
            forged_category.get_data(as_text=True),
        )

        missing_action = self.client.post(
            "/supply/catalog/categories/bulk-assign",
            data={"category_id": "", "product_id": str(product_id)},
            follow_redirects=True,
        )
        self.assertIn(
            "Выберите категорию или действие",
            missing_action.get_data(as_text=True),
        )

        missing_product = self.client.post(
            "/supply/catalog/categories/bulk-assign",
            data={"category_id": "__none__", "product_id": "99999999"},
            follow_redirects=True,
        )
        self.assertIn(
            "Некоторые выбранные товары уже недоступны",
            missing_product.get_data(as_text=True),
        )

    def test_category_mutations_require_admin_login(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(
            self.client.post(
                "/supply/catalog/categories/add", data={"name": self.CATEGORY_NAME}
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                "/supply/catalog/categories/1/delete"
            ).status_code,
            302,
        )
        self.assertEqual(
            self.client.post(
                "/supply/catalog/categories/bulk-assign",
                data={"category_id": "", "product_id": "1"},
            ).status_code,
            302,
        )


if __name__ == "__main__":
    unittest.main()

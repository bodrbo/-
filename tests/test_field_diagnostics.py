import unittest

from support import application_module
from modules.field_diagnostics.constants import (
    DIAGNOSTIC_BLOCKS,
    FIELD_DIAGNOSTIC_QUESTIONS,
)


class FieldDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        application_module.init_db()
        self.client = application_module.app.test_client()
        self.cleanup_database()
        self.addCleanup(self.cleanup_database)

    @staticmethod
    def cleanup_database():
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM field_diagnostic_extra_defects")
            db.execute("DELETE FROM field_diagnostic_answers")
            db.execute("DELETE FROM field_diagnostic_sheets")
            db.execute(
                "DELETE FROM tuning_boat_profiles WHERE model_key LIKE 'test field %'"
            )
            db.execute(
                "DELETE FROM clients WHERE phone IN "
                "('+7 999 123-45-67', '+7 921 000-00-00') "
                "OR client_name LIKE 'Test Field Client %'"
            )
            db.commit()

    def login(self):
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Диагност"

    def create_sheet(self, model="Test Field 520", inspection_type="water"):
        return self.client.post(
            "/tuning/diagnostics/field/add",
            data={
                "boat_model": model,
                "owner_name": "Иван Судовладелец",
                "owner_phone": "+7 999 123-45-67",
                "inspection_type": inspection_type,
            },
        )

    @staticmethod
    def sheet_id_from(response):
        return int(response.headers["Location"].rstrip("/").rsplit("/", 1)[-1])

    def answer_all_questions(self, sheet_id, inspection_type="water"):
        for question_index in range(
            len(FIELD_DIAGNOSTIC_QUESTIONS[inspection_type])
        ):
            response = self.client.post(
                "/tuning/diagnostics/field/%d/answer" % sheet_id,
                data={"question_index": str(question_index), "status": "ok"},
            )
            self.assertEqual(response.status_code, 302)

    def test_routes_require_admin_login(self):
        responses = [
            self.client.get("/tuning/diagnostics/field"),
            self.client.get("/tuning/diagnostics/field/1/edit"),
            self.client.post("/tuning/diagnostics/field/1/delete"),
        ]

        for response in responses:
            self.assertEqual(response.status_code, 302)
            self.assertIn("/admin/login", response.headers["Location"])

    def test_visible_hull_block_has_only_the_two_current_checks(self):
        expected = [
            "Проверка целостности видимого силового набора",
            "Проверка состояния внешнего гелькоута",
        ]

        for inspection_type in ("water", "land"):
            hull_questions = [
                question
                for question in FIELD_DIAGNOSTIC_QUESTIONS[inspection_type]
                if question["section"] == "Видимая часть корпуса"
            ]
            self.assertEqual(
                [question["title"] for question in hull_questions],
                expected,
            )
            self.assertIn("транцевую доску", hull_questions[0]["text"])
            self.assertIn("стрингеры", hull_questions[0]["text"])
            self.assertIn("шпангоуты", hull_questions[0]["text"])

    def test_new_sheet_adds_custom_model_to_shared_catalog(self):
        self.login()

        response = self.create_sheet(model="  Test   Field  520  ")

        self.assertEqual(response.status_code, 302)
        self.assertRegex(
            response.headers["Location"], r"/tuning/diagnostics/field/\d+$"
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            profile = db.execute(
                "SELECT * FROM tuning_boat_profiles WHERE model_key = ?",
                ("test field 520",),
            ).fetchone()
            sheet = db.execute("SELECT * FROM field_diagnostic_sheets").fetchone()
            self.assertIsNotNone(profile)
            self.assertEqual(profile["model_name"], "Test Field 520")
            self.assertEqual(sheet["boat_profile_id"], profile["id"])
            self.assertEqual(sheet["boat_model"], "Test Field 520")
            self.assertEqual(sheet["owner_name"], "Иван Судовладелец")
            self.assertEqual(sheet["owner_phone"], "+7 999 123-45-67")
            self.assertEqual(sheet["created_by_name"], "Диагност")
            self.assertIn('"section": "Электрика"', sheet["question_set_json"])
            owner = db.execute(
                "SELECT * FROM clients WHERE id = ?", (sheet["owner_client_id"],)
            ).fetchone()
            self.assertIsNotNone(owner)
            self.assertEqual(owner["client_name"], "Иван Судовладелец")
            self.assertEqual(owner["phone"], "+7 999 123-45-67")
            self.assertEqual(owner["boat_model"], "Test Field 520")
            self.assertTrue(owner["token"])
            owner_token = owner["token"]

        page = self.client.get(response.headers["Location"])
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Осмотр на воде", html)
        self.assertIn(FIELD_DIAGNOSTIC_QUESTIONS["water"][0]["title"], html)
        self.assertNotIn(FIELD_DIAGNOSTIC_QUESTIONS["land"][0]["title"], html)
        for block_name in DIAGNOSTIC_BLOCKS:
            self.assertIn(block_name, html)

        cabinet = self.client.get("/client/%s" % owner_token)
        self.assertEqual(cabinet.status_code, 200)
        self.assertIn("Иван Судовладелец", cabinet.get_data(as_text=True))

    def test_existing_catalog_model_is_reused_with_canonical_spelling(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO tuning_boat_profiles "
                "(model_key, model_name, specifications, created_at, updated_at) "
                "VALUES ('test field existing', 'Test Field Existing', '', "
                "'2026-08-31 10:00', '2026-08-31 10:00')"
            )
            db.commit()

        response = self.create_sheet(model=" TEST   FIELD existing ")

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM tuning_boat_profiles "
                    "WHERE model_key = 'test field existing'"
                ).fetchone()[0],
                1,
            )
            sheet = db.execute("SELECT * FROM field_diagnostic_sheets").fetchone()
            self.assertEqual(sheet["boat_model"], "Test Field Existing")

    def test_invalid_creation_rerenders_modal_without_writes(self):
        self.login()

        response = self.client.post(
            "/tuning/diagnostics/field/add",
            data={
                "boat_model": "",
                "owner_name": "",
                "owner_phone": "",
                "inspection_type": "air",
            },
        )

        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Проверьте заполнение", html)
        self.assertIn("Укажите модель лодки", html)
        self.assertIn('field-diagnostic-modal is-open', html)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM field_diagnostic_sheets").fetchone()[0],
                0,
            )

    def test_existing_owner_is_selected_by_id_and_verified_by_phone(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            first = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Test Field Client Тёзка', '', '+7 900 100-10-10', "
                "'test-field-owner-first', '2026-08-31 10:00')"
            ).lastrowid
            second = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Test Field Client Тёзка', '', '+7 900 200-20-20', "
                "'test-field-owner-second', '2026-08-31 10:00')"
            ).lastrowid
            db.commit()

        page_html = self.client.get(
            "/tuning/diagnostics/field"
        ).get_data(as_text=True)
        self.assertIn("+7 900 100-10-10", page_html)
        self.assertIn("+7 900 200-20-20", page_html)

        response = self.client.post(
            "/tuning/diagnostics/field/add",
            data={
                "boat_model": "Test Field Owner Boat",
                "owner_client_id": str(second),
                "owner_name": "Test Field Client Тёзка",
                "owner_phone": "+7 900 200-20-20",
                "inspection_type": "water",
            },
        )
        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            sheet = db.execute(
                "SELECT * FROM field_diagnostic_sheets"
            ).fetchone()
            self.assertEqual(sheet["owner_client_id"], second)
            self.assertNotEqual(sheet["owner_client_id"], first)
            self.assertEqual(sheet["owner_phone"], "+7 900 200-20-20")
        sheet_html = self.client.get(
            response.headers["Location"]
        ).get_data(as_text=True)
        self.assertIn(
            "/admin/clients/%d/cabinet" % second,
            sheet_html,
        )

        mismatch = self.client.post(
            "/tuning/diagnostics/field/add",
            data={
                "boat_model": "Test Field Wrong Owner",
                "owner_client_id": str(first),
                "owner_name": "Test Field Client Тёзка",
                "owner_phone": "+7 900 200-20-20",
                "inspection_type": "water",
            },
        )
        self.assertEqual(mismatch.status_code, 400)
        self.assertIn(
            "Номер телефона не совпадает с выбранным клиентом",
            mismatch.get_data(as_text=True),
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM field_diagnostic_sheets"
                ).fetchone()[0],
                1,
            )

    def test_typed_owner_phone_reuses_existing_cabinet_after_normalization(self):
        self.login()
        with application_module.app.app_context():
            db = application_module.get_db()
            client_id = db.execute(
                "INSERT INTO clients "
                "(client_name, boat_model, phone, token, created_at) "
                "VALUES ('Test Field Client Существующий', 'Старая лодка', "
                "'+7 (900) 555-44-33', 'test-field-normalized-phone', "
                "'2026-08-31 10:00')"
            ).lastrowid
            db.commit()

        response = self.client.post(
            "/tuning/diagnostics/field/add",
            data={
                "boat_model": "Test Field New Boat",
                "owner_client_id": "",
                "owner_name": "Другое написание имени",
                "owner_phone": "8 900 555 44 33",
                "inspection_type": "land",
            },
        )

        self.assertEqual(response.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            sheet = db.execute(
                "SELECT * FROM field_diagnostic_sheets"
            ).fetchone()
            self.assertEqual(sheet["owner_client_id"], client_id)
            self.assertEqual(
                sheet["owner_name"], "Test Field Client Существующий"
            )
            self.assertEqual(sheet["owner_phone"], "+7 (900) 555-44-33")
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM clients "
                    "WHERE client_name = 'Test Field Client Существующий'"
                ).fetchone()[0],
                1,
            )

    def test_problem_needs_description_and_answers_are_sequential(self):
        self.login()
        created = self.create_sheet()
        sheet_path = created.headers["Location"]
        sheet_id = int(sheet_path.rstrip("/").rsplit("/", 1)[-1])

        invalid = self.client.post(
            "/tuning/diagnostics/field/%d/answer" % sheet_id,
            data={"question_index": "0", "status": "problem", "comment": ""},
        )
        skipped = self.client.post(
            "/tuning/diagnostics/field/%d/answer" % sheet_id,
            data={"question_index": "2", "status": "ok"},
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertIn("Опишите обнаруженную неисправность", invalid.get_data(as_text=True))
        self.assertEqual(skipped.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM field_diagnostic_answers").fetchone()[0],
                0,
            )

    def test_completed_inspection_builds_pdf_with_ok_and_problem_results(self):
        self.login()
        created = self.create_sheet(inspection_type="land")
        sheet_path = created.headers["Location"]
        sheet_id = int(sheet_path.rstrip("/").rsplit("/", 1)[-1])
        questions = FIELD_DIAGNOSTIC_QUESTIONS["land"]

        for question_index in range(len(questions)):
            status = "problem" if question_index == 1 else "ok"
            response = self.client.post(
                "/tuning/diagnostics/field/%d/answer" % sheet_id,
                data={
                    "question_index": str(question_index),
                    "status": status,
                    "comment": "Трещина 4 см у крепления" if status == "problem" else "",
                },
            )
            self.assertEqual(response.status_code, 302)

        other_step = self.client.get(sheet_path)
        other_html = other_step.get_data(as_text=True)
        self.assertIn("Прочие неисправности", other_html)
        self.assertIn("Завершить осмотр", other_html)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute(
                    "SELECT status FROM field_diagnostic_sheets WHERE id = ?",
                    (sheet_id,),
                ).fetchone()["status"],
                "in_progress",
            )

        completed = self.client.post(
            "/tuning/diagnostics/field/%d/other" % sheet_id,
            data={
                "other_defect[]": [
                    "Не хватает спасательного жилета",
                    "Повреждён тент",
                ]
            },
        )
        self.assertEqual(completed.status_code, 302)

        result = self.client.get(sheet_path)
        result_html = result.get_data(as_text=True)
        self.assertEqual(result.status_code, 200)
        self.assertIn("Диагностический лист готов", result_html)
        self.assertIn("Трещина 4 см у крепления", result_html)
        self.assertIn("Не хватает спасательного жилета", result_html)
        for block_name in DIAGNOSTIC_BLOCKS:
            self.assertIn(block_name, result_html)

        with application_module.app.app_context():
            db = application_module.get_db()
            sheet = db.execute(
                "SELECT * FROM field_diagnostic_sheets WHERE id = ?", (sheet_id,)
            ).fetchone()
            self.assertEqual(sheet["status"], "completed")
            self.assertIsNotNone(sheet["completed_at"])
            self.assertIsNotNone(sheet["other_completed_at"])
            answers = db.execute(
                "SELECT * FROM field_diagnostic_answers WHERE sheet_id = ? "
                "ORDER BY question_index",
                (sheet_id,),
            ).fetchall()
            self.assertEqual(len(answers), len(questions))
            self.assertEqual(answers[1]["status"], "problem")
            self.assertEqual(answers[1]["comment"], "Трещина 4 см у крепления")
            extra_defects = db.execute(
                "SELECT description FROM field_diagnostic_extra_defects "
                "WHERE sheet_id = ? ORDER BY id",
                (sheet_id,),
            ).fetchall()
            self.assertEqual(
                [row["description"] for row in extra_defects],
                ["Не хватает спасательного жилета", "Повреждён тент"],
            )

        pdf = self.client.get(
            "/tuning/diagnostics/field/%d/diagnostic-sheet.pdf" % sheet_id
        )
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertGreater(len(pdf.data), 5000)
        self.assertIn(b"/Subtype /Image", pdf.data)
        self.assertIn(
            'inline; filename="Diagnostic-sheet-%d.pdf"' % sheet_id,
            pdf.headers["Content-Disposition"],
        )

    def test_other_block_can_be_completed_without_defects(self):
        self.login()
        created = self.create_sheet(inspection_type="water")
        sheet_path = created.headers["Location"]
        sheet_id = int(sheet_path.rstrip("/").rsplit("/", 1)[-1])
        questions = FIELD_DIAGNOSTIC_QUESTIONS["water"]
        section_order = []
        for question in questions:
            if not section_order or section_order[-1] != question["section"]:
                section_order.append(question["section"])
        self.assertEqual(section_order, list(DIAGNOSTIC_BLOCKS[:3]))
        self.assertNotIn(
            "Прочее", {question["section"] for question in questions}
        )

        for question_index in range(len(questions)):
            self.client.post(
                "/tuning/diagnostics/field/%d/answer" % sheet_id,
                data={"question_index": str(question_index), "status": "ok"},
            )
        response = self.client.post(
            "/tuning/diagnostics/field/%d/other" % sheet_id,
            data={"other_defect[]": ""},
        )

        self.assertEqual(response.status_code, 302)
        result = self.client.get(sheet_path).get_data(as_text=True)
        self.assertIn("Дополнительные неисправности не указаны", result)
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM field_diagnostic_extra_defects "
                    "WHERE sheet_id = ?",
                    (sheet_id,),
                ).fetchone()[0],
                0,
            )

    def test_pdf_is_not_available_until_inspection_is_completed(self):
        self.login()
        created = self.create_sheet()
        sheet_path = created.headers["Location"]
        sheet_id = int(sheet_path.rstrip("/").rsplit("/", 1)[-1])

        response = self.client.get(
            "/tuning/diagnostics/field/%d/diagnostic-sheet.pdf" % sheet_id
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(sheet_path))

    def test_completed_sheet_can_be_fully_edited(self):
        self.login()
        created = self.create_sheet()
        sheet_id = self.sheet_id_from(created)
        self.answer_all_questions(sheet_id)
        self.client.post(
            "/tuning/diagnostics/field/%d/other" % sheet_id,
            data={"other_defect[]": ["Старое замечание", "Удалить замечание"]},
        )

        edit_page = self.client.get(
            "/tuning/diagnostics/field/%d/edit" % sheet_id
        )
        edit_html = edit_page.get_data(as_text=True)
        self.assertEqual(edit_page.status_code, 200)
        self.assertIn("Редактировать диагностический лист", edit_html)
        self.assertIn("После начала осмотра тип зафиксирован", edit_html)

        with application_module.app.app_context():
            db = application_module.get_db()
            answers = db.execute(
                "SELECT * FROM field_diagnostic_answers WHERE sheet_id = ? "
                "ORDER BY question_index",
                (sheet_id,),
            ).fetchall()
            extras = db.execute(
                "SELECT * FROM field_diagnostic_extra_defects WHERE sheet_id = ? "
                "ORDER BY id",
                (sheet_id,),
            ).fetchall()

        data = {
            "boat_model": "Test Field Edited",
            "owner_name": "Пётр Новый",
            "owner_phone": "+7 921 000-00-00",
            "inspection_type": "water",
            "extra_id[]": [str(extras[0]["id"]), str(extras[1]["id"]), ""],
            "extra_description[]": [
                "Исправленное замечание",
                "",
                "Новое замечание",
            ],
        }
        for answer in answers:
            status = "problem" if answer["question_index"] == 0 else "ok"
            data["answer_status_%d" % answer["id"]] = status
            data["answer_comment_%d" % answer["id"]] = (
                "Не работает главный выключатель" if status == "problem" else ""
            )

        response = self.client.post(
            "/tuning/diagnostics/field/%d/edit" % sheet_id,
            data=data,
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith(
                "/tuning/diagnostics/field/%d" % sheet_id
            )
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            sheet = db.execute(
                "SELECT * FROM field_diagnostic_sheets WHERE id = ?", (sheet_id,)
            ).fetchone()
            profile = db.execute(
                "SELECT * FROM tuning_boat_profiles WHERE id = ?",
                (sheet["boat_profile_id"],),
            ).fetchone()
            changed_answer = db.execute(
                "SELECT * FROM field_diagnostic_answers "
                "WHERE sheet_id = ? AND question_index = 0",
                (sheet_id,),
            ).fetchone()
            saved_extras = db.execute(
                "SELECT description FROM field_diagnostic_extra_defects "
                "WHERE sheet_id = ? ORDER BY id",
                (sheet_id,),
            ).fetchall()

            self.assertEqual(sheet["boat_model"], "Test Field Edited")
            self.assertEqual(sheet["owner_name"], "Пётр Новый")
            self.assertEqual(sheet["owner_phone"], "+7 921 000-00-00")
            self.assertEqual(profile["model_name"], "Test Field Edited")
            self.assertEqual(changed_answer["status"], "problem")
            self.assertEqual(
                changed_answer["comment"], "Не работает главный выключатель"
            )
            self.assertEqual(
                [row["description"] for row in saved_extras],
                ["Исправленное замечание", "Новое замечание"],
            )

        result_html = self.client.get(
            "/tuning/diagnostics/field/%d" % sheet_id
        ).get_data(as_text=True)
        self.assertIn("Test Field Edited", result_html)
        self.assertIn("Не работает главный выключатель", result_html)
        self.assertIn("Исправленное замечание", result_html)
        self.assertNotIn("Удалить замечание", result_html)

    def test_inspection_type_can_only_change_before_answers(self):
        self.login()
        created = self.create_sheet(inspection_type="water")
        sheet_id = self.sheet_id_from(created)
        edit_path = "/tuning/diagnostics/field/%d/edit" % sheet_id
        base_data = {
            "boat_model": "Test Field 520",
            "owner_name": "Иван Судовладелец",
            "owner_phone": "+7 999 123-45-67",
            "inspection_type": "land",
            "extra_id[]": "",
            "extra_description[]": "",
        }

        changed = self.client.post(edit_path, data=base_data)
        self.assertEqual(changed.status_code, 302)
        with application_module.app.app_context():
            db = application_module.get_db()
            sheet = db.execute(
                "SELECT * FROM field_diagnostic_sheets WHERE id = ?", (sheet_id,)
            ).fetchone()
            self.assertEqual(sheet["inspection_type"], "land")
            self.assertIn(
                FIELD_DIAGNOSTIC_QUESTIONS["land"][0]["title"],
                sheet["question_set_json"],
            )

        answered = self.client.post(
            "/tuning/diagnostics/field/%d/answer" % sheet_id,
            data={"question_index": "0", "status": "ok"},
        )
        self.assertEqual(answered.status_code, 302)
        base_data["inspection_type"] = "water"
        blocked = self.client.post(edit_path, data=base_data)

        self.assertEqual(blocked.status_code, 400)
        self.assertIn(
            "Тип осмотра нельзя изменить после первого ответа",
            blocked.get_data(as_text=True),
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            sheet = db.execute(
                "SELECT * FROM field_diagnostic_sheets WHERE id = ?", (sheet_id,)
            ).fetchone()
            self.assertEqual(sheet["inspection_type"], "land")

    def test_delete_sheet_removes_results_but_keeps_catalog_profile(self):
        self.login()
        created = self.create_sheet()
        sheet_id = self.sheet_id_from(created)
        self.client.post(
            "/tuning/diagnostics/field/%d/answer" % sheet_id,
            data={"question_index": "0", "status": "problem", "comment": "Тест"},
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            profile_id = db.execute(
                "SELECT boat_profile_id FROM field_diagnostic_sheets WHERE id = ?",
                (sheet_id,),
            ).fetchone()["boat_profile_id"]
            db.execute(
                "INSERT INTO field_diagnostic_extra_defects "
                "(sheet_id, description, created_at) VALUES (?, 'Тест', '2026-08-31')",
                (sheet_id,),
            )
            db.commit()

        response = self.client.post(
            "/tuning/diagnostics/field/%d/delete" % sheet_id
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            response.headers["Location"].endswith("/tuning/diagnostics/field")
        )
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM field_diagnostic_sheets WHERE id = ?",
                    (sheet_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM field_diagnostic_answers WHERE sheet_id = ?",
                    (sheet_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM field_diagnostic_extra_defects "
                    "WHERE sheet_id = ?",
                    (sheet_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM tuning_boat_profiles WHERE id = ?",
                    (profile_id,),
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()

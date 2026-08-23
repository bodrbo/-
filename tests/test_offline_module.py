import io
import json
import os
import unittest
from unittest.mock import patch

from support import TEST_DIRECTORY, application_module


class OfflineModuleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        application_module.init_db()
        cls.original_static_folder = application_module.app.static_folder
        application_module.app.static_folder = os.path.join(
            TEST_DIRECTORY.name, "offline-static"
        )
        os.makedirs(application_module.app.static_folder, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        application_module.app.static_folder = cls.original_static_folder

    def setUp(self):
        self.client = application_module.app.test_client()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute("DELETE FROM offline_operations")
            db.execute("DELETE FROM checklist_answer_photos")
            db.execute("DELETE FROM boat_checklist_answers")
            db.execute("DELETE FROM boat_checklists")
            db.execute("DELETE FROM boat_defects")
            db.execute("DELETE FROM boat_documents")
            db.commit()

    def log_in(self, employee_name="Дмитрий Тарусов"):
        with application_module.app.app_context():
            account = application_module.get_db().execute(
                "SELECT id, username FROM team_accounts WHERE employee_name = ?",
                (employee_name,),
            ).fetchone()
        self.assertIsNotNone(account)
        with self.client.session_transaction() as session:
            session["team_id"] = account["id"]
            session["team_employee_name"] = employee_name
            session["team_username"] = account["username"]

    def post_operation(self, operation, attachment=None):
        data = {"operation": json.dumps(operation, ensure_ascii=False)}
        if attachment is not None:
            attachment_id, content = attachment
            data["attachments"] = (
                io.BytesIO(content),
                f"{attachment_id}.jpg",
                "image/jpeg",
            )
        return self.client.post(
            "/api/offline/sync", data=data, content_type="multipart/form-data"
        )

    def test_workspace_and_bootstrap_require_a_captain(self):
        response = self.client.get("/team/offline")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/team/login", response.headers["Location"])

        self.log_in("Эльмира Бектаева")
        response = self.client.get("/api/offline/bootstrap")
        self.assertEqual(response.status_code, 403)

        self.log_in()
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO boat_documents "
                "(boat, title, filename, original_filename, uploaded_at) "
                "VALUES ('Ларус', 'Судовой билет', 'ticket.pdf', 'ticket.pdf', '2026-08-23 10:00')"
            )
            db.commit()

        page = self.client.get("/team/offline")
        bootstrap = self.client.get("/api/offline/bootstrap")
        payload = bootstrap.get_json()

        self.assertEqual(page.status_code, 200)
        self.assertIn("Судовой журнал".encode(), page.data)
        self.assertEqual(bootstrap.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["boats"]), 3)
        self.assertTrue(payload["boats"][0]["checklists"]["pre"]["questions"])
        self.assertEqual(payload["boats"][0]["documents"][0]["title"], "Судовой билет")
        self.assertRegex(
            payload["boats"][0]["documents"][0]["url"],
            r"^/team/documents/boat/\d+$",
        )

    def test_manual_defect_replay_is_idempotent(self):
        self.log_in()
        operation = {
            "id": "offline-defect-00000001",
            "type": "defect",
            "status": "queued",
            "created_at": "2026-08-23 11:00",
            "payload": {
                "boat": "Ларус",
                "description": "Не работает помпа",
                "reported_at": "2026-08-23 10:40",
            },
        }
        with patch.object(
            application_module, "send_telegram_notification"
        ) as telegram, patch.object(
            application_module, "send_push_notification"
        ) as push:
            first = self.post_operation(operation)
            second = self.post_operation(operation)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()["created"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.get_json()["created"])
        with application_module.app.app_context():
            db = application_module.get_db()
            defects = db.execute(
                "SELECT * FROM boat_defects WHERE description = 'Не работает помпа'"
            ).fetchall()
            operations = db.execute("SELECT * FROM offline_operations").fetchall()
        self.assertEqual(len(defects), 1)
        self.assertEqual(defects[0]["reported_at"], "2026-08-23 10:40")
        self.assertEqual(len(operations), 1)
        telegram.assert_called_once()
        push.assert_called_once()

    def test_complete_checklist_with_photo_replays_atomically_once(self):
        self.log_in()
        questions = application_module._checklist_questions_for("pre", "Ларус")
        attachment_id = "offline-photo-000000001"
        answers = []
        for index, question in enumerate(questions):
            answers.append(
                {
                    "question_index": index,
                    "question_title": question.get("title") or "",
                    "question_text": question["text"],
                    "status": "problem" if index == 0 else "ok",
                    "comment": "Обнаружена вода" if index == 0 else "",
                    "photo_ids": [attachment_id] if index == 0 else [],
                }
            )
        operation = {
            "id": "offline-checklist-000001",
            "type": "checklist",
            "status": "queued",
            "created_at": "2026-08-23 08:00",
            "payload": {
                "boat": "Ларус",
                "checklist_type": "pre",
                "checklist_label": "Предрейсовый осмотр",
                "started_at": "2026-08-23 08:00",
                "completed_at": "2026-08-23 08:15",
                "questions": questions,
                "answers": answers,
                "extra_defects": ["Скрипит уключина"],
            },
        }

        with patch.object(
            application_module, "send_telegram_notification"
        ) as telegram, patch.object(
            application_module, "send_telegram_photo"
        ) as telegram_photo, patch.object(
            application_module, "send_push_notification"
        ) as push:
            first = self.post_operation(
                operation, (attachment_id, b"test-jpeg-content")
            )
            second = self.post_operation(operation)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.get_json()["created"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.get_json()["created"])
        with application_module.app.app_context():
            db = application_module.get_db()
            checklist = db.execute("SELECT * FROM boat_checklists").fetchone()
            answer_count = db.execute(
                "SELECT COUNT(*) AS count FROM boat_checklist_answers"
            ).fetchone()["count"]
            defects = db.execute(
                "SELECT description FROM boat_defects ORDER BY id"
            ).fetchall()
            photo = db.execute("SELECT * FROM checklist_answer_photos").fetchone()
            operation_count = db.execute(
                "SELECT COUNT(*) AS count FROM offline_operations"
            ).fetchone()["count"]

        self.assertEqual(checklist["completed_at"], "2026-08-23 08:15")
        self.assertEqual(answer_count, len(questions))
        self.assertEqual(len(defects), 2)
        self.assertIn("Обнаружена вода", defects[0]["description"])
        self.assertEqual(defects[1]["description"], "Скрипит уключина")
        self.assertTrue(
            os.path.exists(
                os.path.join(
                    application_module.app.static_folder,
                    "checklist_photos",
                    photo["filename"],
                )
            )
        )
        self.assertEqual(operation_count, 1)
        self.assertEqual(telegram.call_count, 2)
        telegram_photo.assert_called_once()
        self.assertEqual(push.call_count, 2)

    def test_operation_cannot_be_replayed_by_another_employee(self):
        operation = {
            "id": "offline-owner-check-0001",
            "type": "defect",
            "created_at": "2026-08-23 11:00",
            "payload": {
                "boat": "Ларус",
                "description": "Проверка владельца операции",
                "reported_at": "2026-08-23 11:00",
            },
        }
        self.log_in("Дмитрий Тарусов")
        first = self.post_operation(operation)
        self.log_in("Платон Жмаев")
        second = self.post_operation(operation)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertIn("другому сотруднику", second.get_json()["error"])
        with application_module.app.app_context():
            count = application_module.get_db().execute(
                "SELECT COUNT(*) FROM boat_defects "
                "WHERE description = 'Проверка владельца операции'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_invalid_operation_is_rejected_without_partial_rows(self):
        self.log_in()
        operation = {
            "id": "offline-checklist-invalid1",
            "type": "checklist",
            "created_at": "2026-08-23 08:00",
            "payload": {
                "boat": "Ларус",
                "checklist_type": "pre",
                "answers": [
                    {
                        "question_index": 2,
                        "question_text": "Пропущены первые вопросы",
                        "status": "ok",
                        "photo_ids": [],
                    }
                ],
            },
        }
        response = self.post_operation(operation)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["retryable"])
        with application_module.app.app_context():
            db = application_module.get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM boat_checklists").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM offline_operations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

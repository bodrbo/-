import os
import unittest

from support import application_module


def text_response(text="Готово", input_tokens=120, output_tokens=30):
    return {
        "id": "resp_test",
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


class AIAssistantTests(unittest.TestCase):
    ADMIN_USERNAME = "ai-assistant-admin-test"
    EMPLOYEE_NAME = "Сотрудник AI Тест"
    TEAM_USERNAME = "ai-assistant-team-test"

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        self.original_create_response = (
            application_module._openai_responses_client.create_response
        )
        self.original_api_key = os.environ.get("OPENAI_API_KEY")
        os.environ["OPENAI_API_KEY"] = "test-openai-api-key"
        with application_module.app.app_context():
            db = application_module.get_db()
            self._clear(db)
            self.admin_id = db.execute(
                "INSERT INTO admin_accounts "
                "(admin_name, username, password_hash, created_at) "
                "VALUES ('AI Администратор', ?, 'test-hash', '2026-09-05 10:00')",
                (self.ADMIN_USERNAME,),
            ).lastrowid
            self.employee_id = db.execute(
                "INSERT INTO employees (name, created_at, deleted_at) "
                "VALUES (?, '2026-09-05 10:00', NULL)",
                (self.EMPLOYEE_NAME,),
            ).lastrowid
            self.team_id = db.execute(
                "INSERT INTO team_accounts "
                "(employee_id, employee_name, username, password_hash, created_at) "
                "VALUES (?, ?, ?, 'test-hash', '2026-09-05 10:00')",
                (self.employee_id, self.EMPLOYEE_NAME, self.TEAM_USERNAME),
            ).lastrowid
            db.commit()

    def tearDown(self):
        application_module._openai_responses_client.create_response = (
            self.original_create_response
        )
        if self.original_api_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self.original_api_key
        with application_module.app.app_context():
            self._clear(application_module.get_db())

    @classmethod
    def _clear(cls, db):
        owners = db.execute(
            "SELECT id FROM ai_conversations WHERE "
            "(owner_type = 'admin' AND owner_id IN ("
            "SELECT id FROM admin_accounts WHERE username = ?)) OR "
            "(owner_type = 'employee' AND owner_id IN ("
            "SELECT id FROM employees WHERE name = ?))",
            (cls.ADMIN_USERNAME, cls.EMPLOYEE_NAME),
        ).fetchall()
        for row in owners:
            db.execute("DELETE FROM ai_tool_runs WHERE conversation_id = ?", (row["id"],))
            db.execute("DELETE FROM ai_messages WHERE conversation_id = ?", (row["id"],))
            db.execute("DELETE FROM ai_conversations WHERE id = ?", (row["id"],))
        employee_rows = db.execute(
            "SELECT id FROM employees WHERE name = ?", (cls.EMPLOYEE_NAME,)
        ).fetchall()
        for row in employee_rows:
            db.execute("DELETE FROM employee_positions WHERE employee_id = ?", (row["id"],))
            db.execute("DELETE FROM team_accounts WHERE employee_id = ?", (row["id"],))
        db.execute("DELETE FROM employees WHERE name = ?", (cls.EMPLOYEE_NAME,))
        db.execute("DELETE FROM admin_accounts WHERE username = ?", (cls.ADMIN_USERNAME,))
        db.commit()

    def login_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["admin_id"] = self.admin_id
            session["admin_name"] = "AI Администратор"

    def login_employee(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["team_id"] = self.team_id
            session["team_employee_name"] = self.EMPLOYEE_NAME
            session["team_username"] = self.TEAM_USERNAME

    def set_position(self, position):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT OR IGNORE INTO employee_positions "
                "(employee_id, position, created_at) VALUES (?, ?, '2026-09-05 10:00')",
                (self.employee_id, position),
            )
            db.commit()

    def test_page_and_api_require_staff_login(self):
        self.assertIn("/admin/login", self.client.get("/assistant").headers["Location"])
        response = self.client.post("/assistant/api/chat", json={"message": "Привет"})
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["ok"])

    def test_missing_api_key_keeps_application_running_and_returns_json_503(self):
        self.login_admin()
        os.environ.pop("OPENAI_API_KEY", None)
        page = self.client.get("/assistant")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Помощник ещё не подключён", page.get_data(as_text=True))
        response = self.client.post("/assistant/api/chat", json={"message": "Привет"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("OPENAI_API_KEY", response.get_json()["error"])

    def test_admin_chat_is_stored_locally_and_uses_safe_responses_payload(self):
        self.login_admin()
        payloads = []

        def fake_create(payload):
            payloads.append(payload)
            return text_response("За последние семь дней всё спокойно.")

        application_module._openai_responses_client.create_response = fake_create
        response = self.client.post(
            "/assistant/api/chat",
            json={"message": "Дай краткую сводку за неделю"},
        )
        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["message"]["content"], "За последние семь дней всё спокойно.")
        self.assertFalse(payloads[0]["store"])
        self.assertNotIn("test-openai-api-key", str(payloads[0]))
        self.assertIn("safety_identifier", payloads[0])
        tool_names = {tool["name"] for tool in payloads[0]["tools"]}
        self.assertIn("get_business_overview", tool_names)
        self.assertIn("get_tuning_summary", tool_names)

        with application_module.app.app_context():
            db = application_module.get_db()
            messages = db.execute(
                "SELECT role, content, input_tokens, output_tokens FROM ai_messages "
                "WHERE conversation_id = ? ORDER BY id",
                (data["conversation_id"],),
            ).fetchall()
        self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["input_tokens"], 120)
        self.assertEqual(messages[1]["output_tokens"], 30)

    def test_function_call_reads_aggregates_and_is_audited(self):
        self.login_admin()
        payloads = []

        def fake_create(payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "id": "resp_tool",
                    "output": [{
                        "type": "function_call",
                        "call_id": "call_schedule",
                        "name": "get_schedule_summary",
                        "arguments": '{"date_from":"2026-09-05","date_to":"2026-09-05"}',
                    }],
                    "usage": {"input_tokens": 80, "output_tokens": 10},
                }
            function_outputs = [
                item for item in payload["input"]
                if item.get("type") == "function_call_output"
            ]
            self.assertEqual(function_outputs[0]["call_id"], "call_schedule")
            self.assertIn('"planned_revenue_rub"', function_outputs[0]["output"])
            return text_response("В расписании на этот день рейсов нет.", 50, 15)

        application_module._openai_responses_client.create_response = fake_create
        response = self.client.post(
            "/assistant/api/chat", json={"message": "Что в расписании 5 сентября?"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["message"]["usage"]["input_tokens"], 130)
        with application_module.app.app_context():
            audit = application_module.get_db().execute(
                "SELECT tool_name, arguments_json, result_json FROM ai_tool_runs "
                "WHERE conversation_id = ?",
                (data["conversation_id"],),
            ).fetchone()
        self.assertEqual(audit["tool_name"], "get_schedule_summary")
        self.assertIn("2026-09-05", audit["arguments_json"])
        self.assertIn('"ok":true', audit["result_json"])

    def test_employee_tool_scope_changes_with_position(self):
        self.set_position("Менеджер по работе с клиентами")
        self.login_employee()
        captured = []
        application_module._openai_responses_client.create_response = lambda payload: (
            captured.append(payload) or text_response("Доступ проверен")
        )
        response = self.client.post(
            "/assistant/api/chat", json={"message": "Что мне доступно?"}
        )
        self.assertEqual(response.status_code, 200)
        names = {tool["name"] for tool in captured[0]["tools"]}
        self.assertIn("get_schedule_summary", names)
        self.assertIn("get_clients_summary", names)
        self.assertIn("get_payroll_summary", names)
        self.assertNotIn("get_tuning_summary", names)
        self.assertNotIn("get_business_overview", names)
        page = self.client.get("/assistant")
        self.assertIn("Менеджер по работе с клиентами", page.get_data(as_text=True))

    def test_user_cannot_open_or_append_to_another_users_conversation(self):
        self.login_admin()
        created = self.client.post("/assistant/api/conversations").get_json()
        conversation_id = created["conversation_id"]
        self.login_employee()
        application_module._openai_responses_client.create_response = lambda payload: text_response()
        response = self.client.post(
            "/assistant/api/chat",
            json={"conversation_id": conversation_id, "message": "Чужой диалог"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.delete(
            f"/assistant/api/conversations/{conversation_id}"
        ).status_code, 404)

    def test_invalid_payload_is_rejected_without_openai_call(self):
        self.login_admin()
        calls = []
        application_module._openai_responses_client.create_response = lambda payload: calls.append(payload)
        self.assertEqual(self.client.post("/assistant/api/chat", data="text").status_code, 400)
        self.assertEqual(self.client.post("/assistant/api/chat", json={"message": "  "}).status_code, 400)
        self.assertEqual(self.client.post(
            "/assistant/api/chat", json={"message": "x" * 4001}
        ).status_code, 400)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

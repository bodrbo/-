import json
import os
import unittest
from unittest.mock import patch

from support import application_module
from modules.ai_assistant.tools import execute_tool


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
        tuning_order_ids = [
            row["id"] for row in db.execute(
                "SELECT id FROM tuning_orders WHERE source_ref LIKE 'ai-summary-test:%'"
            ).fetchall()
        ]
        for order_id in tuning_order_ids:
            db.execute("DELETE FROM tuning_payments WHERE order_id = ?", (order_id,))
            db.execute("DELETE FROM tuning_orders WHERE id = ?", (order_id,))
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
            db.execute(
                "DELETE FROM employee_telegram_accounts WHERE employee_id = ?",
                (row["id"],),
            )
            db.execute("DELETE FROM employee_positions WHERE employee_id = ?", (row["id"],))
            db.execute("DELETE FROM team_accounts WHERE employee_id = ?", (row["id"],))
        db.execute("DELETE FROM employees WHERE name = ?", (cls.EMPLOYEE_NAME,))
        db.execute("DELETE FROM admin_accounts WHERE username = ?", (cls.ADMIN_USERNAME,))
        db.commit()

    def test_tuning_summary_uses_business_date_and_separates_money_metrics(self):
        with application_module.app.app_context():
            db = application_module.get_db()

            def add_order(source_ref, order_date, created_at, total, status, channel):
                return db.execute(
                    "INSERT INTO tuning_orders "
                    "(client_name, boat_model, sale_channel, phone, subtotal, total, "
                    "status, order_date, source_ref, created_at, updated_at) "
                    "VALUES ('AI Test Client', 'AI Test Boat', ?, '', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        channel,
                        total,
                        total,
                        status,
                        order_date,
                        source_ref,
                        created_at,
                        created_at,
                    ),
                ).lastrowid

            june_first = add_order(
                "ai-summary-test:june-1", "2026-06-05", "2026-09-01 10:00",
                100000, "in_progress", "direct",
            )
            june_second = add_order(
                "ai-summary-test:june-2", "2026-06-20", "2026-09-02 10:00",
                50000, "done", "aggregator",
            )
            july_order = add_order(
                "ai-summary-test:july", "2026-07-03", "2026-07-03 10:00",
                70000, "estimate", "direct",
            )
            for order_id, amount, paid_at in (
                (june_first, 40000, "2026-06-10 12:00"),
                (june_first, 10000, "2026-07-01 12:00"),
                (june_second, 50000, "2026-06-25 12:00"),
                (july_order, 20000, "2026-06-30 12:00"),
            ):
                db.execute(
                    "INSERT INTO tuning_payments "
                    "(order_id, amount, paid_at, created_at) VALUES (?, ?, ?, ?)",
                    (order_id, amount, paid_at, paid_at),
                )
            db.commit()

            result = execute_tool(
                db,
                {
                    "owner_type": "admin",
                    "owner_id": self.admin_id,
                    "name": "AI Администратор",
                    "positions": [],
                },
                application_module.BOATS,
                "get_tuning_summary",
                {"date_from": "2026-06-01", "date_to": "2026-06-30"},
            )

        self.assertEqual(result["date_basis"], "order_date")
        self.assertEqual(result["orders"], 2)
        self.assertEqual(result["orders_total_rub"], 150000)
        self.assertEqual(result["payments_for_selected_orders_rub"], 100000)
        self.assertEqual(result["current_outstanding_for_selected_orders_rub"], 50000)
        self.assertEqual(result["payments_received_in_period"], 3)
        self.assertEqual(result["payments_received_in_period_rub"], 110000)
        self.assertEqual(result["by_status"], {"in_progress": 1, "done": 1})
        channels = {
            row["sale_channel"]: row for row in result["sale_channel_breakdown"]
        }
        self.assertEqual(channels["direct"]["orders_total_rub"], 100000)
        self.assertEqual(channels["direct"]["sale_channel_label"], "Напрямую")
        self.assertEqual(channels["aggregator"]["orders_total_rub"], 50000)

    def test_bar_chart_uses_server_calculated_tuning_values_and_business_dates(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            for source_ref, order_date, total in (
                ("ai-summary-test:chart-june-1", "2026-06-05", 100000),
                ("ai-summary-test:chart-june-2", "2026-06-20", 50000),
                ("ai-summary-test:chart-july", "2026-07-03", 70000),
            ):
                db.execute(
                    "INSERT INTO tuning_orders "
                    "(client_name, boat_model, sale_channel, phone, subtotal, total, "
                    "status, order_date, source_ref, created_at, updated_at) "
                    "VALUES ('AI Chart Client', 'AI Chart Boat', 'direct', '', ?, ?, "
                    "'in_progress', ?, ?, '2026-09-05 12:00', '2026-09-05 12:00')",
                    (total, total, order_date, source_ref),
                )
            db.commit()

            result = execute_tool(
                db,
                {
                    "owner_type": "admin",
                    "owner_id": self.admin_id,
                    "name": "AI Администратор",
                    "positions": [],
                },
                application_module.BOATS,
                "get_bar_chart",
                {
                    "subject": "tuning",
                    "metric": "amount_rub",
                    "group_by": "month",
                    "date_from": "2026-06-01",
                    "date_to": "2026-07-31",
                },
            )

        chart = result["visualization"]
        self.assertEqual(chart["type"], "bar")
        self.assertEqual(chart["value_format"], "currency")
        self.assertEqual(chart["labels"], ["июн 2026", "июл 2026"])
        self.assertEqual(chart["datasets"][0]["data"], [150000, 70000])
        self.assertIn("01.06.2026", chart["subtitle"])

    def test_admin_employee_directory_returns_roles_and_safe_link_states(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            db.execute(
                "INSERT INTO employee_positions (employee_id, position, created_at) "
                "VALUES (?, 'Тестовая должность AI', '2026-09-05 10:00')",
                (self.employee_id,),
            )
            db.execute(
                "INSERT INTO employee_telegram_accounts "
                "(employee_id, chat_id, username, display_name, linked_at) "
                "VALUES (?, 'sensitive-chat-123', 'secret-user', 'AI Test', "
                "'2026-09-05 10:00')",
                (self.employee_id,),
            )
            db.commit()

            admin = {
                "owner_type": "admin",
                "owner_id": self.admin_id,
                "name": "AI Администратор",
                "positions": [],
            }
            result = execute_tool(
                db,
                admin,
                application_module.BOATS,
                "get_employees_directory",
                {"position": "Тестовая должность AI"},
            )

        self.assertEqual(result["employees_total"], 1)
        self.assertEqual(result["by_position"], {"Тестовая должность AI": 1})
        self.assertEqual(result["accounts_created"], 1)
        self.assertEqual(result["telegram_linked"], 1)
        self.assertEqual(result["directory"], [{
            "name": self.EMPLOYEE_NAME,
            "positions": ["Тестовая должность AI"],
            "active": True,
            "account_created": True,
            "telegram_linked": True,
        }])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(self.TEAM_USERNAME, serialized)
        self.assertNotIn("test-hash", serialized)
        self.assertNotIn("sensitive-chat-123", serialized)
        self.assertNotIn("secret-user", serialized)

    def test_employee_cannot_access_employee_directory(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            with self.assertRaisesRegex(ValueError, "недоступен"):
                execute_tool(
                    db,
                    {
                        "owner_type": "employee",
                        "owner_id": self.employee_id,
                        "name": self.EMPLOYEE_NAME,
                        "positions": [],
                    },
                    application_module.BOATS,
                    "get_employees_directory",
                    {},
                )

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
        self.assertIn("avatars/botsman-ai.jpeg", page.get_data(as_text=True))
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
        self.assertIn("get_data_catalog", tool_names)
        self.assertIn("get_business_overview", tool_names)
        self.assertIn("get_tuning_summary", tool_names)
        self.assertIn("get_employees_directory", tool_names)

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

    def test_bar_chart_is_returned_stored_and_rendered_in_conversation_history(self):
        self.login_admin()
        payloads = []

        def fake_create(payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return {
                    "id": "resp_chart",
                    "output": [{
                        "type": "function_call",
                        "call_id": "call_chart",
                        "name": "get_bar_chart",
                        "arguments": (
                            '{"subject":"tuning","metric":"orders",'
                            '"group_by":"status","date_from":"2026-06-01",'
                            '"date_to":"2026-06-30"}'
                        ),
                    }],
                    "usage": {"input_tokens": 80, "output_tokens": 10},
                }
            return text_response("Построил график заказов по статусам.", 50, 15)

        application_module._openai_responses_client.create_response = fake_create
        response = self.client.post(
            "/assistant/api/chat",
            json={"message": "Покажи график тюнинг-заказов за июнь"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        charts = data["message"]["visualizations"]
        self.assertEqual(len(charts), 1)
        self.assertEqual(charts[0]["type"], "bar")

        with application_module.app.app_context():
            stored = application_module.get_db().execute(
                "SELECT visualizations_json FROM ai_messages "
                "WHERE conversation_id = ? AND role = 'assistant'",
                (data["conversation_id"],),
            ).fetchone()
        self.assertEqual(json.loads(stored["visualizations_json"]), charts)

        page = self.client.get(
            f"/assistant?conversation={data['conversation_id']}"
        ).get_data(as_text=True)
        self.assertIn("data-ai-chart=", page)
        self.assertIn("vendor/chart.umd.min.js", page)
        self.assertIn("График · данные системы", page)

    def test_repeated_tool_call_is_cached_and_forced_to_a_text_answer(self):
        self.login_admin()
        payloads = []
        executions = []

        def fake_create(payload):
            payloads.append(payload)
            if "tools" in payload:
                call_number = len(payloads)
                return {
                    "id": f"resp_repeat_{call_number}",
                    "output": [{
                        "type": "function_call",
                        "call_id": f"call_tuning_{call_number}",
                        "name": "get_tuning_summary",
                        "arguments": '{"date_from":"2026-06-01","date_to":"2026-06-30"}',
                    }],
                    "usage": {"input_tokens": 20, "output_tokens": 5},
                }
            return text_response("За июнь найдено два заказа.", 25, 10)

        def fake_execute(db, user, boats, name, arguments):
            executions.append((name, arguments))
            return {"orders": 2, "orders_total_rub": 150000}

        application_module._openai_responses_client.create_response = fake_create
        with patch(
            "modules.ai_assistant.services.execute_tool", side_effect=fake_execute
        ):
            response = self.client.post(
                "/assistant/api/chat",
                json={"message": "Сколько было тюнинг-заказов за июнь?"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["message"]["content"],
            "За июнь найдено два заказа.",
        )
        self.assertEqual(len(executions), 1)
        self.assertEqual(len(payloads), 3)
        self.assertIn("tools", payloads[0])
        self.assertIn("tools", payloads[1])
        self.assertNotIn("tools", payloads[2])
        self.assertNotIn("tool_choice", payloads[2])

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
        self.assertNotIn("get_employees_directory", names)
        catalog_tool = next(
            tool for tool in captured[0]["tools"] if tool["name"] == "get_data_catalog"
        )
        catalog_ids = set(
            catalog_tool["parameters"]["properties"]["dataset"]["enum"]
        )
        self.assertIn("schedule", catalog_ids)
        self.assertNotIn("tuning_orders", catalog_ids)
        chart_tool = next(
            tool for tool in captured[0]["tools"] if tool["name"] == "get_bar_chart"
        )
        chart_subjects = set(
            chart_tool["parameters"]["properties"]["subject"]["enum"]
        )
        self.assertEqual(chart_subjects, {"schedule", "clients", "payroll"})
        page = self.client.get("/assistant")
        self.assertIn("Менеджер по работе с клиентами", page.get_data(as_text=True))

    def test_bar_chart_enforces_data_scope_for_employee(self):
        with application_module.app.app_context():
            db = application_module.get_db()
            user = {
                "owner_type": "employee",
                "owner_id": self.employee_id,
                "name": self.EMPLOYEE_NAME,
                "positions": [],
            }
            with self.assertRaisesRegex(ValueError, "тюнинга доступна только"):
                execute_tool(
                    db,
                    user,
                    application_module.BOATS,
                    "get_bar_chart",
                    {
                        "subject": "tuning",
                        "metric": "orders",
                        "group_by": "month",
                    },
                )
            payroll = execute_tool(
                db,
                user,
                application_module.BOATS,
                "get_bar_chart",
                {
                    "subject": "payroll",
                    "metric": "amount_rub",
                    "group_by": "employee",
                },
            )
        self.assertEqual(payroll["visualization"]["type"], "bar")

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

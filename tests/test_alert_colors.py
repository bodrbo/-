import pathlib
import re
import unittest

from werkzeug.datastructures import MultiDict

from support import application_module


class AlertColorTests(unittest.TestCase):
    """Notices that report success or something neutral are green; errors
    stay red. The tuning schedule marks them with .alert / .alert.success."""

    def css(self):
        return (pathlib.Path(application_module.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")

    def rule(self, selector):
        match = re.search(r"^" + re.escape(selector) + r"\s*\{([^}]*)\}", self.css(), re.MULTILINE)
        self.assertIsNotNone(match, selector)
        return match.group(1)

    def test_a_plain_alert_is_the_red_error_box(self):
        body = self.rule(".alert")
        self.assertIn("var(--danger)", body)

    def test_a_success_alert_is_green_everywhere(self):
        body = self.rule(".alert.success")
        self.assertIn("var(--success)", body)
        self.assertNotIn("var(--danger)", body)
        # declared after the base rule, so it wins on equal specificity
        css = self.css()
        self.assertGreater(css.index("\n.alert.success {"), css.index("\n.alert {"))

    def test_the_tuning_schedule_marks_success_and_error_notices_differently(self):
        app = application_module.app
        app.config.update(TESTING=True)
        client = app.test_client()
        for kind, expected, unexpected in (("success", 'class="alert success"', 'class="alert" '),
                                           ("error", 'class="alert"', 'alert success')):
            with client.session_transaction() as session:
                session["admin_id"] = 1
                session["admin_name"] = "Администратор"
                session["schedule_notice"] = {"message": "Сообщение", "type": kind}
            page = client.get("/schedule/tuning").get_data(as_text=True)
            notice = page.split('role="status">Сообщение')[0].rsplit("<p ", 1)[1]
            self.assertIn(expected, "<p " + notice, kind)
            self.assertNotIn(unexpected, "<p " + notice, kind)

    def test_the_script_notice_uses_the_same_classes(self):
        template = (pathlib.Path(application_module.__file__).parent / "templates" / "schedule" / "tuning_index.html"
                    ).read_text(encoding="utf-8")
        self.assertIn("notice.className = 'alert' + (success ? ' success' : '');", template)


if __name__ == "__main__":
    unittest.main()

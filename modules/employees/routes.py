"""Administrator HTTP interface for the employee directory."""

from flask import Blueprint, redirect, render_template, request, session, url_for

from integrations.telegram import TelegramAPIError

from . import repository, services


def create_employees_blueprint(
    get_db,
    admin_login_required,
    telegram_contacts_fetcher,
    telegram_sender,
    telegram_configured,
    telegram_bot_username,
):
    blueprint = Blueprint("employees", __name__)

    def redirect_with_notice(message, success):
        session["employees_notice"] = {
            "message": message,
            "type": "success" if success else "error",
        }
        return redirect(url_for("employees.index"))

    @blueprint.route("/employees")
    @admin_login_required
    def index():
        db = get_db()
        contacts = services.telegram_contacts(db)
        return render_template(
            "employees/index.html",
            employees=services.employee_directory(db),
            known_positions=repository.list_known_positions(db),
            telegram_contacts=contacts,
            unlinked_telegram_contacts=[
                contact for contact in contacts if contact["linked_employee_id"] is None
            ],
            telegram_configured=telegram_configured(),
            telegram_bot_username=(telegram_bot_username() or "").lstrip("@"),
            notice=session.pop("employees_notice", None),
            active_page="employees",
        )

    @blueprint.route("/employees/<int:employee_id>/positions", methods=["POST"])
    @admin_login_required
    def add_position(employee_id):
        success, message = services.add_position(
            get_db(), employee_id, request.form.get("position", "")
        )
        return redirect_with_notice(message, success)

    @blueprint.route(
        "/employees/<int:employee_id>/positions/<int:position_id>/delete",
        methods=["POST"],
    )
    @admin_login_required
    def delete_position(employee_id, position_id):
        success, message = services.delete_position(get_db(), employee_id, position_id)
        return redirect_with_notice(message, success)

    @blueprint.route("/employees/telegram/sync", methods=["POST"])
    @admin_login_required
    def sync_telegram():
        try:
            contacts = telegram_contacts_fetcher()
        except TelegramAPIError as error:
            return redirect_with_notice(str(error), False)
        synced = services.sync_telegram_contacts(get_db(), contacts)
        if synced:
            return redirect_with_notice(
                f"Получены данные {synced} Telegram-аккаунтов. Теперь выберите нужный аккаунт у сотрудника.",
                True,
            )
        return redirect_with_notice(
            "Новых сообщений боту не найдено. Попросите сотрудника открыть бота и нажать Start.",
            False,
        )

    @blueprint.route("/employees/<int:employee_id>/telegram", methods=["POST"])
    @admin_login_required
    def link_telegram(employee_id):
        success, message = services.link_telegram_account(
            get_db(), employee_id, request.form.get("chat_id", "")
        )
        return redirect_with_notice(message, success)

    @blueprint.route(
        "/employees/<int:employee_id>/telegram/unlink", methods=["POST"]
    )
    @admin_login_required
    def unlink_telegram(employee_id):
        success, message = services.unlink_telegram_account(get_db(), employee_id)
        return redirect_with_notice(message, success)

    @blueprint.route("/employees/<int:employee_id>/telegram/test", methods=["POST"])
    @admin_login_required
    def test_telegram(employee_id):
        success, message = services.send_test_notification(
            get_db(), employee_id, telegram_sender
        )
        return redirect_with_notice(message, success)

    return blueprint

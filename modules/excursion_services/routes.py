"""Administrator routes for the excursion services catalog."""

from flask import Blueprint, redirect, render_template, request, session, url_for

from . import repository, services


def create_blueprint(get_db, admin_login_required):
    blueprint = Blueprint("excursion_services", __name__)

    def redirect_with_notice(message, success, anchor=None):
        session["excursion_services_notice"] = {
            "message": message,
            "type": "success" if success else "error",
        }
        location = url_for("excursion_services.index")
        if anchor:
            location += f"#{anchor}"
        return redirect(location)

    @blueprint.route("/services")
    @admin_login_required
    def index():
        return render_template(
            "excursion_services/index.html",
            services=repository.list_services(get_db()),
            notice=session.pop("excursion_services_notice", None),
            create_values=session.pop("excursion_services_create_values", {}),
            active_page="services",
        )

    @blueprint.route("/services", methods=["POST"])
    @admin_login_required
    def create_service():
        success, message, result = services.create_service(get_db(), request.form)
        if not success:
            session["excursion_services_create_values"] = result or {}
        return redirect_with_notice(
            message, success, f"service-{result}" if success else "new-service"
        )

    @blueprint.route("/services/<int:service_id>", methods=["POST"])
    @admin_login_required
    def update_service(service_id):
        success, message, _data = services.update_service(
            get_db(), service_id, request.form
        )
        return redirect_with_notice(message, success, f"service-{service_id}")

    return blueprint

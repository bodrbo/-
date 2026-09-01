"""Administrator routes for the excursion services catalog."""

from flask import Blueprint, redirect, render_template, request, session, url_for

from . import repository, services
from .constants import SERVICE_TYPES


def create_blueprint(get_db, access_required, is_manager_view, boats):
    blueprint = Blueprint("excursion_services", __name__)

    def redirect_with_notice(message, success, section="group", anchor=None):
        session["excursion_services_notice"] = {
            "message": message,
            "type": "success" if success else "error",
        }
        location = url_for("excursion_services.index", section=section)
        if anchor:
            location += f"#{anchor}"
        return redirect(location)

    @blueprint.route("/services")
    @access_required
    def index():
        section = request.args.get("section", "group")
        if section not in SERVICE_TYPES:
            section = "group"
        catalog = repository.list_services(get_db())
        return render_template(
            "excursion_services/index.html",
            services=[
                service for service in catalog
                if service["service_type"] == section
            ],
            service_counts={
                service_type: sum(
                    service["service_type"] == service_type
                    for service in catalog
                )
                for service_type in SERVICE_TYPES
            },
            service_types=SERVICE_TYPES,
            section=section,
            boats=boats,
            notice=session.pop("excursion_services_notice", None),
            create_values=session.pop("excursion_services_create_values", {}),
            active_page="services",
            manager_view=is_manager_view(),
        )

    @blueprint.route("/services", methods=["POST"])
    @access_required
    def create_service():
        section = request.form.get("service_type", "group")
        success, message, result = services.create_service(
            get_db(), request.form, boats
        )
        if not success:
            session["excursion_services_create_values"] = result or {}
        return redirect_with_notice(
            message, success, section,
            f"service-{result}" if success else "new-service",
        )

    @blueprint.route("/services/<int:service_id>", methods=["POST"])
    @access_required
    def update_service(service_id):
        existing = repository.get_service(get_db(), service_id)
        section = (
            existing["service_type"] if existing is not None else "group"
        )
        success, message, data = services.update_service(
            get_db(), service_id, request.form, boats
        )
        if success and data is not None:
            section = data["service_type"]
        return redirect_with_notice(
            message, success, section, f"service-{service_id}"
        )

    return blueprint

"""Administrator routes for Зарплаты -> Ставки."""

from flask import Blueprint, redirect, render_template, request, session, url_for

from . import repository, services
from .constants import BOAT_RATE_ROLES, EXCURSION_ROLES


def create_payroll_rates_blueprint(get_db, access_required, list_boats=None, recalculate_boat_rates=None):
    blueprint = Blueprint("payroll_rates", __name__)

    @blueprint.route("/payroll/rates")
    @access_required
    def rates_index():
        return redirect(url_for("payroll_rates.excursions"))

    @blueprint.route("/payroll/rates/excursions")
    @access_required
    def excursions():
        db = get_db()
        return render_template(
            "payroll/rates.html",
            section="excursions",
            rates=repository.list_excursion_role_rates(db),
            roles=EXCURSION_ROLES,
            boat_rate_roles=BOAT_RATE_ROLES,
            boat_rates={role: repository.list_boat_rates(db, role) for role in BOAT_RATE_ROLES},
            boat_rates_applied={role: repository.boat_rates_applied(db, role) for role in BOAT_RATE_ROLES},
            boats=list_boats(db) if list_boats else [],
            active_page="payroll",
            sub_page="rates",
            notice=session.pop("payroll_rates_notice", None),
        )

    @blueprint.route("/payroll/rates/excursions", methods=["POST"])
    @access_required
    def update_excursion_rate():
        success, message = services.set_excursion_role_rate(
            get_db(), request.form.get("role", ""), request.form.get("rate", ""),
        )
        session["payroll_rates_notice"] = {
            "message": message,
            "type": "success" if success else "error",
        }
        return redirect(url_for("payroll_rates.excursions"))

    @blueprint.route("/payroll/rates/excursions/boats", methods=["POST"])
    @access_required
    def update_boat_rates():
        db = get_db()
        boats = request.form.getlist("boat[]")
        raw_rates = list(zip(boats, request.form.getlist("boat_rate[]")))
        success, message = services.save_boat_rates(
            db, request.form.get("role", ""), raw_rates,
            set(list_boats(db)) if list_boats else set(),
            recalculate_boat_rates or (lambda *args: ""),
        )
        session["payroll_rates_notice"] = {
            "message": message,
            "type": "success" if success else "error",
        }
        return redirect(url_for("payroll_rates.excursions"))

    @blueprint.route("/payroll/rates/tuning")
    @access_required
    def tuning():
        return render_template(
            "payroll/rates.html",
            section="tuning",
            active_page="payroll",
            sub_page="rates",
            notice=None,
        )

    return blueprint

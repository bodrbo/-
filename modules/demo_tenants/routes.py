"""Platform-admin screen for creating/branding demo accounts, and the
public login those demo accounts use — see modules/demo_tenants/__init__.py."""

import os
import secrets

from flask import Blueprint, redirect, render_template, request, session, url_for

from . import repository, services
from .constants import DEMO_LOGO_EXTENSIONS, DEMO_MODULES


def create_blueprint(
    get_db, admin_login_required, tenant_db_dir, tenant_logo_dir,
    provision_tenant_db, seed_tenant_db,
):
    blueprint = Blueprint("demo_tenants", __name__)

    @blueprint.route("/demo-admin/tenants")
    @admin_login_required
    def index():
        db = get_db()
        tenants = [
            dict(row, modules=services.enabled_module_set(row))
            for row in repository.list_tenants(db)
        ]
        return render_template(
            "demo_tenants_index.html", active_page="demo_tenants",
            tenants=tenants, demo_modules=DEMO_MODULES,
            notice=session.pop("demo_tenant_notice", None),
            error=session.pop("demo_tenant_error", None),
            credentials=session.pop("demo_tenant_credentials", None),
        )

    @blueprint.route("/demo-admin/tenants", methods=["POST"])
    @admin_login_required
    def create():
        db = get_db()
        logo_filename = None
        file = request.files.get("logo")
        if file and file.filename:
            ext = os.path.splitext(file.filename)[1].lower()
            if ext in DEMO_LOGO_EXTENSIONS:
                os.makedirs(tenant_logo_dir, exist_ok=True)
                logo_filename = f"{secrets.token_hex(8)}{ext}"
                file.save(os.path.join(tenant_logo_dir, logo_filename))

        success, message, credentials = services.create_tenant(
            db,
            request.form.get("company_name", ""),
            request.form.getlist("modules"),
            request.form.get("accent_color", ""),
            logo_filename,
            tenant_db_dir,
            provision_tenant_db,
            seed_tenant_db if request.form.get("seed_demo_data") else None,
        )
        if success:
            session["demo_tenant_notice"] = message
            session["demo_tenant_credentials"] = credentials
        else:
            session["demo_tenant_error"] = message
        return redirect(url_for("demo_tenants.index"))

    @blueprint.route("/demo-admin/tenants/<int:tenant_id>/delete", methods=["POST"])
    @admin_login_required
    def delete(tenant_id):
        db = get_db()
        success, message = services.delete_tenant(db, tenant_id)
        session["demo_tenant_notice" if success else "demo_tenant_error"] = message
        return redirect(url_for("demo_tenants.index"))

    @blueprint.route("/demo/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if session.get("demo_tenant_id"):
                return redirect(url_for("index"))
            return render_template("demo_tenant_login.html", error=None)

        db = get_db()
        tenant = services.authenticate(
            db, request.form.get("username", ""), request.form.get("password", ""),
        )
        if tenant is None:
            return render_template(
                "demo_tenant_login.html", error="Неверный логин или пароль.",
            ), 401

        session.clear()
        session["demo_tenant_id"] = tenant["id"]
        session["demo_tenant_name"] = tenant["company_name"]
        session["demo_tenant_db_path"] = tenant["db_path"]
        session["demo_tenant_modules"] = tenant["enabled_modules"]
        session["demo_tenant_logo"] = tenant["logo_filename"]
        session["demo_tenant_accent_color"] = tenant["accent_color"]
        return redirect(url_for("index"))

    @blueprint.route("/demo/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("demo_tenants.login"))

    return blueprint

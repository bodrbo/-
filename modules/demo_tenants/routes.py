"""Platform-admin screen for creating/branding demo accounts, and the
public login those demo accounts use — see modules/demo_tenants/__init__.py."""

import os
import secrets

from flask import Blueprint, redirect, render_template, request, session, url_for

from . import repository, services
from .constants import DEMO_LOGO_EXTENSIONS, DEMO_MODULES, DEMO_TOUR_STEPS


def create_blueprint(
    get_db, get_shared_db, admin_login_required, tenant_db_dir, tenant_logo_dir,
    provision_tenant_db, seed_tenant_db, client_ip,
):
    blueprint = Blueprint("demo_tenants", __name__)

    def _save_uploaded_logo(field_name):
        file = request.files.get(field_name)
        if not (file and file.filename):
            return None
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in DEMO_LOGO_EXTENSIONS:
            return None
        os.makedirs(tenant_logo_dir, exist_ok=True)
        filename = f"{secrets.token_hex(8)}{ext}"
        file.save(os.path.join(tenant_logo_dir, filename))
        return filename

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
        logo_filename = _save_uploaded_logo("logo")
        empty_state_logo_filename = _save_uploaded_logo("empty_state_logo")
        favicon_filename = _save_uploaded_logo("favicon")

        success, message, credentials = services.create_tenant(
            db,
            request.form.get("company_name", ""),
            request.form.getlist("modules"),
            request.form.get("accent_color", ""),
            logo_filename,
            empty_state_logo_filename,
            favicon_filename,
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

    @blueprint.route("/demo-admin/tenants/<int:tenant_id>/reset", methods=["POST"])
    @admin_login_required
    def reset(tenant_id):
        db = get_db()
        success, message = services.reset_tenant_data(
            db, tenant_id, provision_tenant_db,
            seed_tenant_db if request.form.get("seed_demo_data") else None,
        )
        session["demo_tenant_notice" if success else "demo_tenant_error"] = message
        return redirect(url_for("demo_tenants.index"))

    @blueprint.route("/demo-admin/tenants/<int:tenant_id>/edit")
    @admin_login_required
    def edit(tenant_id):
        db = get_db()
        tenant = repository.get_tenant(db, tenant_id)
        if tenant is None:
            return redirect(url_for("demo_tenants.index"))
        return render_template(
            "demo_tenant_edit.html", active_page="demo_tenants",
            tenant=dict(tenant, modules=services.enabled_module_set(tenant)),
            demo_modules=DEMO_MODULES,
            error=session.pop("demo_tenant_error", None),
        )

    @blueprint.route("/demo-admin/tenants/<int:tenant_id>/edit", methods=["POST"])
    @admin_login_required
    def update(tenant_id):
        db = get_db()
        tenant = repository.get_tenant(db, tenant_id)
        if tenant is None:
            return redirect(url_for("demo_tenants.index"))

        new_logo_filename = _save_uploaded_logo("logo")
        new_empty_state_logo_filename = _save_uploaded_logo("empty_state_logo")
        new_favicon_filename = _save_uploaded_logo("favicon")
        success, message = services.update_tenant(
            db, tenant_id,
            request.form.get("company_name", ""),
            request.form.getlist("modules"),
            request.form.get("accent_color", ""),
            new_logo_filename,
            new_empty_state_logo_filename,
            new_favicon_filename,
            request.form.get("username", ""),
            request.form.get("password", ""),
        )
        # Only drop the old file once the new one is safely referenced by a
        # successful save — an upload that fails validation elsewhere in
        # the form shouldn't silently orphan the tenant's current logo.
        if success:
            for new_filename, old_filename in (
                (new_logo_filename, tenant["logo_filename"]),
                (new_empty_state_logo_filename, tenant["empty_state_logo_filename"]),
                (new_favicon_filename, tenant["favicon_filename"]),
            ):
                if new_filename and old_filename:
                    old_path = os.path.join(tenant_logo_dir, old_filename)
                    try:
                        if os.path.exists(old_path):
                            os.remove(old_path)
                    except OSError:
                        pass

        if success:
            session["demo_tenant_notice"] = message
            return redirect(url_for("demo_tenants.index"))
        session["demo_tenant_error"] = message
        return redirect(url_for("demo_tenants.edit", tenant_id=tenant_id))

    @blueprint.route("/demo/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if session.get("demo_tenant_id"):
                return redirect(url_for("index"))
            return render_template("demo_tenant_login.html", error=None)

        # Always the shared DB, never get_db() — a browser submitting this
        # form while an old demo session cookie is still around would
        # otherwise have get_db() resolve to THAT tenant's own isolated
        # database (session["demo_tenant_db_path"]), where no
        # demo_tenants row exists at all, and every login would 401.
        db = get_shared_db()
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
        session["demo_tenant_empty_state_logo"] = tenant["empty_state_logo_filename"]
        session["demo_tenant_favicon"] = tenant["favicon_filename"]
        session["demo_tenant_accent_color"] = tenant["accent_color"]
        if services.note_login_ip(db, tenant["id"], client_ip()):
            session["demo_tour_step"] = 0
            return redirect(url_for(DEMO_TOUR_STEPS[0]["endpoint"]))
        return redirect(url_for("index"))

    @blueprint.route("/demo/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("demo_tenants.login"))

    @blueprint.route("/tour/launch")
    def tour_launch():
        """Manually (re)starts the guided tour from step 1 — the "Запустить
        обучение" link in the "?" feedback widget (_software_request_widget
        .html), available to any signed-in staff session, demo or real:
        the automatic launch on a never-seen IP (see note_login_ip) is
        demo-only, but walking through the tour on demand is not."""
        session["demo_tour_step"] = 0
        return redirect(url_for(DEMO_TOUR_STEPS[0]["endpoint"]))

    @blueprint.route("/tour/next")
    def tour_next():
        """A plain link, not a form: advancing the tour has no server-side
        effect besides the session counter, so there's nothing a GET here
        could destroy — matches the "просто перейти по ссылке" feel the
        tour is built around. Works the same for a demo tenant session and
        a real admin/employee session — both just carry demo_tour_step."""
        step = session.get("demo_tour_step")
        if step is None:
            return redirect(url_for("index"))
        next_step = step + 1
        if next_step >= len(DEMO_TOUR_STEPS):
            session.pop("demo_tour_step", None)
            return redirect(url_for("index"))
        session["demo_tour_step"] = next_step
        return redirect(url_for(DEMO_TOUR_STEPS[next_step]["endpoint"]))

    @blueprint.route("/tour/skip")
    def tour_skip():
        session.pop("demo_tour_step", None)
        return redirect(request.referrer or url_for("index"))

    return blueprint

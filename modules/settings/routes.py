"""Admin screen for editing system_settings (currently: VAT/fiscal rates)."""

import datetime as dt

from flask import Blueprint, redirect, render_template, request, session, url_for

from . import repository
from .constants import SETTINGS_GROUPS, all_settings


def create_blueprint(get_db, admin_login_required):
    blueprint = Blueprint("settings", __name__)

    def render_index(**extra):
        db = get_db()
        stored = repository.get_all(db)
        groups = []
        for group in SETTINGS_GROUPS:
            fields = []
            for key, definition in group["settings"].items():
                fields.append({
                    "key": key,
                    "label": definition["label"],
                    "hint": definition.get("hint", ""),
                    "choices": definition["choices"],
                    "allow_empty": definition.get("allow_empty", False),
                    "empty_label": definition.get("empty_label", "— не задано —"),
                    "value": stored.get(key, definition["default"]),
                })
            groups.append({
                "id": group["id"], "title": group["title"],
                "hint": group.get("hint", ""), "fields": fields,
            })
        return render_template(
            "settings_general.html",
            groups=groups,
            active_page="settings",
            notice=session.pop("settings_notice", None),
            error=session.pop("settings_error", None),
            **extra,
        )

    @blueprint.route("/settings")
    @admin_login_required
    def index():
        return render_index()

    @blueprint.route("/settings", methods=["POST"])
    @admin_login_required
    def update():
        db = get_db()
        definitions = all_settings()
        to_save = {}
        for key, definition in definitions.items():
            raw_value = request.form.get(key, "").strip()
            valid_values = {value for value, _label in definition["choices"]}
            if not raw_value:
                if definition.get("allow_empty"):
                    to_save[key] = ""
                    continue
                session["settings_error"] = (
                    f"Заполните поле «{definition['label']}»."
                )
                return redirect(url_for("settings.index"))
            if raw_value not in valid_values:
                session["settings_error"] = (
                    f"Недопустимое значение для «{definition['label']}»."
                )
                return redirect(url_for("settings.index"))
            to_save[key] = raw_value

        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        for key, value in to_save.items():
            repository.set_value(db, key, value, now)
        db.commit()
        session["settings_notice"] = "Настройки сохранены."
        return redirect(url_for("settings.index"))

    return blueprint

"""Settings page with the copies, plus the cron route that makes one."""

import datetime as dt

from flask import (
    Blueprint, abort, redirect, render_template, request, send_from_directory,
    session, url_for,
)

from . import services
from .constants import OFFSITE_MANUAL_BUDGET_SECONDS
from .yandex_disk import read_offsite_status


def _size_text(size):
    if size < 1048576:
        return "%d КБ" % max(1, round(size / 1024.0))
    return "%.1f МБ" % (size / 1048576.0)


def create_blueprint(db_path, backup_dir, admin_only_no_demo_tenant, cron_secret, offsite=lambda: None):
    blueprint = Blueprint("db_backup", __name__)

    @blueprint.route("/settings/backups")
    @admin_only_no_demo_tenant
    def index():
        backups = services.list_backups(backup_dir)
        for item in backups:
            item["when"] = item["moment"].strftime("%d.%m.%Y %H:%M")
            item["size_text"] = _size_text(item["size"])
        status = services.read_status(backup_dir)
        stale = not backups or dt.datetime.now() - backups[0]["moment"] > dt.timedelta(hours=48)
        return render_template(
            "settings_backups.html",
            backups=backups,
            status=status,
            stale=stale,
            offsite_enabled=offsite() is not None,
            offsite_status=read_offsite_status(backup_dir),
            backup_dir=backup_dir,
            active_page="settings",
            notice=session.pop("backup_notice", None),
            error=session.pop("backup_error", None),
        )

    @blueprint.route("/settings/backups/run", methods=["POST"])
    @admin_only_no_demo_tenant
    def run_now():
        result = services.run_backup(
            db_path, backup_dir, force=True, offsite=offsite(), offsite_budget=OFFSITE_MANUAL_BUDGET_SECONDS
        )
        if result is None:
            session["backup_error"] = "Копия уже создаётся — подождите минуту и обновите страницу."
        elif not result["ok"]:
            session["backup_error"] = "Копию создать не удалось: %s." % result["message"]
        else:
            session["backup_notice"] = "Копия создана: %s (%s)." % (result["name"], _size_text(result["size"]))
            sent = result["offsite"]
            if sent is not None and not sent["ok"]:
                session["backup_error"] = "На Яндекс.Диск отправить не удалось: %s." % sent["message"]
        return redirect(url_for("db_backup.index"))

    @blueprint.route("/settings/backups/<name>")
    @admin_only_no_demo_tenant
    def download(name):
        if services.parse_name(name) is None or not any(
            item["name"] == name for item in services.list_backups(backup_dir)
        ):
            abort(404)
        response = send_from_directory(backup_dir, name, as_attachment=True)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @blueprint.route("/internal/cron/backup")
    def cron_backup():
        """A copy when one is due (add force=1 to make one anyway). Protected by
        CRON_SECRET like the other /internal/cron endpoints; optional — the site
        also starts a due copy by itself."""
        secret = cron_secret()
        if not secret or request.args.get("token") != secret:
            return "forbidden", 403
        result = services.run_backup(db_path, backup_dir, force=request.args.get("force") == "1", offsite=offsite())
        if result is None:
            return "ok: nothing to do", 200
        if not result["ok"]:
            return "error: %s" % result["message"], 500
        sent = result["offsite"]
        if sent is not None and not sent["ok"]:
            return "ok: %s; yandex disk error: %s" % (result["name"] or "no new copy", sent["message"]), 500
        return "ok: %s" % (result["name"] or "no new copy"), 200

    return blueprint

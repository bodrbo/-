"""Per-account landing pages selected from the user's rendered navigation."""

import json
from html.parser import HTMLParser
from urllib.parse import urlsplit

from flask import Blueprint, jsonify, redirect, request, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS navigation_preferences (
        owner_type TEXT NOT NULL,
        owner_id INTEGER NOT NULL,
        landing_url TEXT NOT NULL,
        PRIMARY KEY (owner_type, owner_id)
    )""")


class NavigationLinks(HTMLParser):
    """Only actual section navigation can grant a signed destination."""

    def __init__(self):
        super().__init__()
        self.navigation = False
        self.links = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "nav":
            classes = set(attrs.get("class", "").split())
            self.navigation = bool(classes & {"main-nav", "sub-tabs", "team-mobile-nav"})
        if tag == "a" and self.navigation:
            href = attrs.get("href", "")
            parsed = urlsplit(href)
            if href.startswith("/") and not parsed.netloc and not parsed.scheme:
                self.links.add(href)

    def handle_endtag(self, tag):
        if tag == "nav":
            self.navigation = False


def register(app, get_db, current_owner):
    blueprint = Blueprint("navigation_preferences", __name__)

    def serializer():
        return URLSafeTimedSerializer(app.secret_key, salt="navigation-preferences-v1")

    def preference(owner):
        row = get_db().execute(
            "SELECT landing_url FROM navigation_preferences WHERE owner_type=? AND owner_id=?",
            owner,
        ).fetchone()
        return row["landing_url"] if row else None

    def valid_destination(href):
        try:
            parsed = urlsplit(href)
            if not href.startswith("/") or parsed.netloc or parsed.scheme or "\\" in href:
                return False
            rule, _ = app.url_map.bind_to_environ(request.environ).match(
                parsed.path, method="GET", return_rule=True,
            )
            return not rule.arguments and rule.endpoint not in {
                "home", "static", "admin_login", "team_login", "investor_login",
                "demo_tenants.login",
            }
        except Exception:
            return False

    @blueprint.route("/api/me/start-page", methods=["POST"])
    def save():
        owner = current_owner()
        if owner is None:
            return jsonify(ok=False, error="Войдите в систему."), 401
        # Same-origin JSON requests plus signed, account-bound destinations.
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify(ok=False, error="Недопустимый источник запроса."), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, error="Некорректный запрос."), 400
        try:
            payload = serializer().loads(data.get("token", ""), max_age=86400)
        except (BadSignature, TypeError):
            return jsonify(ok=False, error="Обновите страницу и повторите выбор."), 400
        if payload.get("owner") != list(owner):
            return jsonify(ok=False, error="Настройка относится к другому пользователю."), 403
        href = payload.get("url")
        if href is not None and not valid_destination(href):
            return jsonify(ok=False, error="Этот экран нельзя выбрать стартовым."), 400
        db = get_db()
        # Compatible with the older SQLite versions on Beget.
        db.execute("DELETE FROM navigation_preferences WHERE owner_type=? AND owner_id=?", owner)
        if href is not None:
            db.execute("INSERT INTO navigation_preferences VALUES (?, ?, ?)", (*owner, href))
        db.commit()
        return jsonify(ok=True, url=href)

    app.register_blueprint(blueprint)

    @app.after_request
    def navigation_response(response):
        if request.endpoint == "static" or request.path.startswith("/api/"):
            return response
        owner = current_owner()
        if owner is None:
            return response
        landing = preference(owner)
        if request.endpoint == "home" or (
            request.endpoint in {"admin_login", "team_login", "investor_login", "demo_tenants.login"}
            and response.status_code in (302, 303)
        ):
            if landing and valid_destination(landing):
                return redirect(landing)
        if response.status_code != 200 or response.mimetype != "text/html" or response.is_streamed:
            return response
        body = response.get_data(as_text=True)
        parser = NavigationLinks()
        parser.feed(body)
        links = {href: serializer().dumps({"owner": owner, "url": href})
                 for href in parser.links if valid_destination(href)}
        if not links:
            return response
        config = json.dumps({
            "endpoint": url_for("navigation_preferences.save"), "links": links,
            "current": landing, "reset": serializer().dumps({"owner": owner, "url": None}),
        }, ensure_ascii=False).replace("<", "\\u003c")
        script = ('<script id="navigation-preferences-data" type="application/json">'
                  + config + '</script><script src="'
                  + url_for("static", filename="navigation-preferences.js") + '" defer></script>')
        response.set_data(body.replace("</body>", script + "</body>"))
        return response

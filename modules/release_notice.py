"""One acknowledgement per account and release, shared across devices."""
import hashlib
import json
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer


def init_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS release_acknowledgements (
        owner_type TEXT NOT NULL, owner_id INTEGER NOT NULL,
        release_id TEXT NOT NULL, acknowledged_at TEXT NOT NULL,
        PRIMARY KEY (owner_type, owner_id, release_id)
    )''')


def register(app, get_db, staff_owner):
    app.config['RELEASE_NOTICE'] = json.loads(
        (Path(app.root_path) / 'release_notes.json').read_text(encoding='utf-8'))
    bp = Blueprint('release_notice', __name__)

    def signer():
        return URLSafeTimedSerializer(app.secret_key, salt='release-notice-v1')

    def owner():
        # Public client pages are identified by their existing bearer link.
        if request.path.startswith('/client/') and (request.view_args or {}).get('token'):
            row = get_db().execute('SELECT id FROM clients WHERE token = ?',
                                   (request.view_args['token'],)).fetchone()
            return ('client', row['id']) if row else None
        return staff_owner()

    @bp.post('/api/me/release-acknowledgement')
    def acknowledge():
        if request.headers.get('Sec-Fetch-Site') == 'cross-site':
            return jsonify(error='Недопустимый источник запроса.'), 403
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error='Некорректный запрос.'), 400
        try:
            payload = signer().loads(data.get('token', ''), max_age=30 * 86400)
        except (BadSignature, TypeError):
            return jsonify(error='Обновите страницу и повторите.'), 400
        account = payload.get('owner')
        release = app.config['RELEASE_NOTICE']
        if payload.get('release') != release['id']:
            return jsonify(error='Доступно новое обновление. Обновите страницу.'), 409
        # A signed client grant can only acknowledge this announcement, and
        # never authorizes reading or changing anything in the client's cabinet.
        if not isinstance(account, list) or len(account) != 2:
            return jsonify(error='Некорректный пользователь.'), 400
        if account[0] == 'client':
            if get_db().execute('SELECT id FROM clients WHERE id=?', (account[1],)).fetchone() is None:
                return jsonify(error='Пользователь не найден.'), 403
        elif list(staff_owner() or ()) != account:
            return jsonify(error='Войдите в свою учётную запись.'), 403
        db = get_db()
        exists = db.execute('SELECT 1 FROM release_acknowledgements WHERE owner_type=? AND owner_id=? AND release_id=?',
                            (*account, release['id'])).fetchone()
        if not exists:
            db.execute("INSERT OR IGNORE INTO release_acknowledgements VALUES (?, ?, ?, datetime('now'))",
                       (*account, release['id']))
            db.commit()
        return jsonify(ok=True)

    app.register_blueprint(bp)

    @app.after_request
    def inject_notice(response):
        if (request.method != 'GET' or response.status_code != 200 or
                response.mimetype != 'text/html' or response.is_streamed or response.direct_passthrough or
                request.endpoint == 'static' or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or
                request.headers.get('Sec-Fetch-Dest', 'document') not in ('', 'document')):
            return response
        body = response.get_data(as_text=True)
        if '</body>' not in body:
            return response
        account = owner()
        release = app.config.get('RELEASE_NOTICE')
        if not account or not release:
            return response
        if get_db().execute('SELECT 1 FROM release_acknowledgements WHERE owner_type=? AND owner_id=? AND release_id=?',
                            (*account, release['id'])).fetchone():
            return response
        audience = account[0] if account[0] in ('client', 'investor') else 'staff'
        items = release.get(audience, release['staff'])
        logo = 'logo-dark.svg'
        company = 'Бодрый Боцман'
        if session.get('demo_tenant_id'):
            company = session.get('demo_tenant_name') or 'Демо'
            filename = session.get('demo_tenant_empty_state_logo') or session.get('demo_tenant_logo')
            logo = 'demo_logos/' + filename if filename else None
        config = {
            'token': signer().dumps({'owner': account, 'release': release['id']}),
            'endpoint': url_for('release_notice.acknowledge'),
            'storageKey': 'release-seen-' + hashlib.sha256(
                json.dumps([account, release['id']]).encode()).hexdigest()[:24],
        }
        markup = render_template('_release_notice.html', release=release, release_items=items,
                                 release_logo=url_for('static', filename=logo) if logo else None,
                                 release_company=company, release_config=config)
        response.set_data(body.replace('</body>', markup + '</body>', 1))
        return response

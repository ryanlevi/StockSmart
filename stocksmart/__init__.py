import hmac
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

from .db import Database
from .inventory import product_url, validate_url
from .mail import mail_error, send_message
from .stores import STORES

LABELS = {'pending': 'Waiting for first check', 'checking': 'Checking stock',
          'available': 'In stock', 'unavailable': 'Out of stock',
          'blocked': 'Blocked by Micro Center', 'unknown': 'Could not confirm',
          'error': 'Check failed', 'paused': 'Paused', 'notified': 'Email sent', 'email_error': 'Email failed'}


def create_app(data_dir=None):
    app = Flask(__name__)
    db = Database(data_dir)
    app.config.update(SECRET_KEY=db.key, MAX_CONTENT_LENGTH=32 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
    app.extensions['stocksmart_db'] = db

    @app.before_request
    def csrf_check():
        if 'csrf' not in session:
            session['csrf'] = secrets.token_urlsafe(32)
        if request.method == 'POST':
            token = request.form.get('csrf_token', '')
            if not hmac.compare_digest(session['csrf'], token):
                abort(400, 'This form expired. Reload the page and try again.')

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.context_processor
    def common():
        with db.connect() as conn:
            heartbeat = conn.execute('SELECT heartbeat FROM worker_health WHERE id=1').fetchone()
        return {'stores': sorted(STORES.items(), key=lambda item: item[1]), 'store_names': STORES,
                'labels': LABELS, 'email_enabled': db.settings()['enabled'],
                'worker_online': bool(heartbeat and time.time() - heartbeat['heartbeat'] < 180),
                'csrf_token': session.get('csrf'), 'product_url': product_url,
                'retailer': db.retailer(), 'now': time.time(), 'schedule': db.schedule(),
                'last_batch': db.latest_batch(), 'transport': db.settings()['transport']}

    @app.template_filter('timestamp')
    def timestamp(value):
        if not value:
            return 'Not checked yet'
        return datetime.fromtimestamp(value, timezone.utc).isoformat()

    @app.get('/')
    def dashboard():
        alerts = db.alerts()
        query = request.args.get('q', '').strip().lower()
        mode = request.args.get('filter', 'all')
        shown = [a for a in alerts if (not query or query in (a['title'] + STORES[a['store_id']]).lower())
                 and (mode == 'all' or (mode == 'openbox' and a['open_box']) or (mode == 'paused' and not a['enabled']))]
        return render_template('dashboard.html', alerts=shown, all_alerts=alerts, query=request.args.get('q', ''),
                               mode=mode, interval=db.settings()['interval'] // 60,
                               active=sum(a['enabled'] for a in alerts),
                               available=sum(a['status'] == 'available' and a['enabled'] for a in alerts),
                               open_count=sum(a['open_box'] for a in alerts))

    def require_alert(alert_id):
        alert = db.alert(alert_id)
        if not alert:
            abort(404)
        return alert

    def save_alert(alert_id=None):
        previous = require_alert(alert_id) if alert_id else None
        values = request.form
        try:
            store_id = values.get('store_id', '')
            if store_id not in STORES:
                raise ValueError('Choose a Micro Center store from the list.')
            clean, product_id = validate_url(values.get('url', ''))
            open_box = int(values.get('open_box') == 'on')
            slug = clean.rstrip('/').split('/')[-1]
            title = slug.replace('-', ' ').strip().capitalize() if not slug.isdigit() else 'Product ' + product_id
            if len(title) > 220:
                title = title[:220]
            with db.connect() as conn:
                if previous:
                    if previous['status'] == 'checking':
                        raise ValueError('A check is in progress. Try editing again after it finishes.')
                    changed = conn.execute("UPDATE alerts SET url=?,product_id=?,title=?,store_id=?,open_box=?,status=?,detail=?,quantity=NULL,price=NULL,next_check=0,last_checked=NULL,last_sent_signature=NULL,notification_error=NULL,check_requested=1,request_reason='edited',last_transport=NULL WHERE id=? AND status!='checking'",
                                 (clean, product_id, title, store_id, open_box, 'pending', 'Waiting for the next check.', alert_id))
                    if changed.rowcount != 1:
                        raise ValueError('A check is in progress. Try editing again after it finishes.')
                else:
                    conn.execute('INSERT INTO alerts(url,product_id,title,store_id,open_box,created_at,check_requested) VALUES(?,?,?,?,?,?,1)',
                                 (clean, product_id, title, store_id, open_box, time.time()))
            flash('Alert updated.' if previous else 'Alert created. The first stock check is queued.', 'success')
            return redirect(url_for('dashboard'))
        except (ValueError, sqlite3.IntegrityError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else 'You already have this product, store, and stock type tracked.'
            flash(message, 'error')
            return render_template('alert_form.html', alert=previous, values=values), 400

    @app.route('/alerts/new', methods=['GET', 'POST'])
    def new_alert():
        if request.method == 'POST':
            return save_alert()
        return render_template('alert_form.html', alert=None, values={'store_id': '101'})

    @app.route('/alerts/<int:alert_id>/edit', methods=['GET', 'POST'])
    def edit_alert(alert_id):
        alert = require_alert(alert_id)
        if request.method == 'POST':
            return save_alert(alert_id)
        return render_template('alert_form.html', alert=alert, values={**alert, 'open_box': 'on' if alert['open_box'] else ''})

    @app.post('/alerts/<int:alert_id>/<action>')
    def alert_action(alert_id, action):
        alert = require_alert(alert_id)
        with db.connect() as conn:
            if action == 'delete':
                conn.execute('DELETE FROM alerts WHERE id=?', (alert_id,))
                flash('Alert deleted.', 'success')
            elif action == 'toggle':
                conn.execute("UPDATE alerts SET enabled=?,check_requested=?,request_reason='resumed' WHERE id=?", (int(not alert['enabled']), int(not alert['enabled']), alert_id))
                flash('Alert paused.' if alert['enabled'] else 'Alert resumed.', 'success')
            elif action == 'check':
                # Respect a 60-second manual check cooldown and an in-flight check.
                cooldown = conn.execute('SELECT blocked_until,last_request FROM retailer_state WHERE id=1').fetchone()
                if not alert['enabled']:
                    flash('Resume this alert before checking it.', 'error')
                elif cooldown['blocked_until'] > time.time():
                    flash('Micro Center is in a shared cooldown. Your alert will retry when it ends; repeated manual checks will not bypass it.', 'info')
                elif alert['status'] == 'checking' or (alert['last_checked'] and time.time() - alert['last_checked'] < 60 and cooldown['last_request'] > 0):
                    flash('A check is running or just completed. Try again in a minute.', 'info')
                else:
                    conn.execute("UPDATE alerts SET check_requested=1,request_reason='manual',status='pending' WHERE id=?", (alert_id,))
                    flash('Stock check queued.', 'success')
            else:
                abort(404)
        return redirect(url_for('dashboard'))

    @app.get('/alerts/<int:alert_id>/history')
    def history(alert_id):
        alert = require_alert(alert_id)
        with db.connect() as conn:
            events = conn.execute('SELECT * FROM events WHERE alert_id=? ORDER BY id DESC LIMIT 50', (alert_id,)).fetchall()
        return render_template('history.html', alert=alert, events=events)

    @app.route('/settings', methods=['GET', 'POST'])
    def settings():
        values = db.settings()
        if request.method == 'POST':
            form = request.form
            try:
                try:
                    port = int(form.get('port', '587'))
                    interval = int(form.get('interval', '300'))
                except ValueError:
                    raise ValueError('Choose a valid SMTP port and check interval.')
                if not 1 <= port <= 65535 or interval not in {300, 600, 900, 1800, 3600}:
                    raise ValueError('Choose a valid SMTP port and check interval.')
                security = form.get('security', '')
                if security not in {'starttls', 'ssl'}:
                    raise ValueError('Choose STARTTLS or SSL/TLS for email encryption.')
                candidate = {key: form.get(key, '').strip() for key in ['host', 'username', 'sender', 'recipient']}
                if any('\n' in value or '\r' in value or len(value) > 254 for value in candidate.values()):
                    raise ValueError('Email settings contain an invalid value.')
                if candidate['host'] and not re.fullmatch(r'[a-zA-Z0-9.-]+', candidate['host']):
                    raise ValueError('Enter an SMTP hostname, such as smtp.gmail.com.')
                for field in ['sender', 'recipient']:
                    if candidate[field] and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+', candidate[field]):
                        raise ValueError('Enter valid sender and recipient email addresses.')
                password = form.get('password') or values['password']
                if form.get('clear_password') == 'on':
                    password = ''
                transport = form.get('transport', values['transport'])
                if transport not in {'http', 'auto'}:
                    raise ValueError('Choose an HTTP checking method.')
                candidate.update(enabled=form.get('enabled') == 'on', port=port, security=security,
                                 interval=interval, password=password, transport=transport)
                if candidate['enabled'] and not all(candidate[k] for k in ['host', 'sender', 'recipient']):
                    raise ValueError('Add the SMTP host, sender, and recipient to enable email.')
                db.save_settings(candidate)
                if form.get('action') == 'test':
                    try:
                        send_message(candidate, 'StockSmart test email', 'Your global StockSmart email configuration is working.\nAll alerts use these settings.')
                    except Exception as exc:
                        flash('Settings saved. ' + mail_error(exc), 'error')
                    else:
                        flash('Settings saved. Test email sent.', 'success')
                else:
                    flash('Global email settings saved.', 'success')
                return redirect(url_for('settings'))
            except ValueError as exc:
                flash(str(exc), 'error')
                # Keep submitted non-secret values on validation errors.
                values = {**values, **{k: form.get(k, '') for k in ['host', 'username', 'sender', 'recipient', 'port', 'security', 'interval', 'transport']},
                          'enabled': form.get('enabled') == 'on'}
                return render_template('settings.html', settings=values), 400
        return render_template('settings.html', settings=values)

    @app.post('/checks/reset-cooldown')
    def reset_cooldown():
        db.clear_cooldown()
        flash('Cooldown cleared. No check was queued. Use Check now on the alert you want to test.', 'success')
        return redirect(url_for('dashboard'))

    @app.get('/health')
    def health():
        with db.connect() as conn:
            heartbeat = conn.execute('SELECT heartbeat FROM worker_health WHERE id=1').fetchone()
        return {'web': 'ok', 'worker': 'ok' if heartbeat and time.time() - heartbeat['heartbeat'] < 180 else 'offline',
                'retailer': 'cooldown' if db.retailer()['blocked_until'] > time.time() else 'ready',
                'next_scheduled_batch': db.schedule()['next_run'], 'last_batch': db.latest_batch()}

    return app

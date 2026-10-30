import fcntl
import logging
import signal
import time

from .db import Database
from .inventory import BrowserChecker, CheckFailure
from .mail import mail_error, notify

log = logging.getLogger(__name__)


def run_check(db, alert, checker, sender=notify, clock=time.time):
    now = clock()
    with db.connect() as conn:
        conn.execute("UPDATE alerts SET status='checking' WHERE id=? AND enabled=1", (alert['id'],))
    try:
        result = checker.check(alert)
    except Exception as exc:
        if isinstance(exc, CheckFailure):
            status, message = exc.status, str(exc)
        else:
            log.exception('Inventory check failed for alert %s', alert['id'])
            status, message = 'error', 'The inventory check failed. Will retry automatically.'
        now = clock()
        interval = db.settings()['interval']
        # Back off repeated verification/network failures rather than hammering the retailer.
        delay = max(interval, 900) if status == 'blocked' else interval
        with db.connect() as conn:
            conn.execute('UPDATE alerts SET status=?,detail=?,last_checked=?,next_check=? WHERE id=? AND enabled=1',
                         (status, message, now, now + delay, alert['id']))
            if conn.execute('SELECT 1 FROM alerts WHERE id=?', (alert['id'],)).fetchone():
                db.event(conn, alert['id'], status, message, now)
        return
    now = clock()
    settings = db.settings()
    status = 'available' if result.available else 'unavailable'
    with db.connect() as conn:
        # UI may have deleted or paused the alert while Chrome was checking.
        current = conn.execute('SELECT * FROM alerts WHERE id=? AND enabled=1', (alert['id'],)).fetchone()
        if not current:
            return
        conn.execute('UPDATE alerts SET title=?,status=?,detail=?,quantity=?,price=?,last_checked=?,next_check=? WHERE id=?',
                     (result.title, status, result.detail, result.quantity, result.price, now,
                      now + settings['interval'], alert['id']))
        db.event(conn, alert['id'], status, result.detail, now)
        if not result.available:
            conn.execute('UPDATE alerts SET last_sent_signature=NULL,notification_error=NULL WHERE id=?', (alert['id'],))
    if result.available and current['last_sent_signature'] != result.signature:
        if not settings['enabled']:
            with db.connect() as conn:
                conn.execute('UPDATE alerts SET notification_error=? WHERE id=?',
                             ('Stock found. Enable global email settings to receive notifications.', alert['id']))
            return
        try:
            sender(settings, alert, result)
        except Exception as exc:
            message = mail_error(exc)
            with db.connect() as conn:
                conn.execute('UPDATE alerts SET notification_error=? WHERE id=?', (message, alert['id']))
                if conn.execute('SELECT 1 FROM alerts WHERE id=?', (alert['id'],)).fetchone():
                    db.event(conn, alert['id'], 'email_error', message, now)
        else:
            with db.connect() as conn:
                conn.execute('UPDATE alerts SET last_sent_signature=?,last_notified=?,notification_error=NULL WHERE id=?',
                             (result.signature, now, alert['id']))
                if conn.execute('SELECT 1 FROM alerts WHERE id=?', (alert['id'],)).fetchone():
                    db.event(conn, alert['id'], 'notified', 'Stock alert email sent.', now)


def main():
    logging.basicConfig(level=logging.INFO)
    db = Database()
    # Only one polling worker can own this data directory.
    lock = open(db.directory / 'worker.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    checker = BrowserChecker()
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Restart recovery: a killed browser check must not remain "checking" forever.
    with db.connect() as conn:
        conn.execute("UPDATE alerts SET status='pending',next_check=0 WHERE status='checking'")
    while running:
        now = time.time()
        with db.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO worker_health(id,heartbeat) VALUES(1,?)', (now,))
            row = conn.execute('SELECT * FROM alerts WHERE enabled=1 AND next_check<=? ORDER BY next_check,id LIMIT 1', (now,)).fetchone()
        if row:
            run_check(db, dict(row), checker)
        else:
            time.sleep(2)


if __name__ == '__main__':
    main()

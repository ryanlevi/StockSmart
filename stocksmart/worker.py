import fcntl
import logging
import signal
import time

from .db import Database
from .inventory import CheckFailure
from .mail import mail_error, notify
from .scheduler import claim_batch, finish_batch
from .sources import BatchChecker

log = logging.getLogger(__name__)


def run_check(db, alert, checker, sender=notify, clock=time.time, next_run=None):
    now = clock()
    with db.connect() as conn:
        current = conn.execute('SELECT * FROM alerts WHERE id=? AND enabled=1', (alert['id'],)).fetchone()
        if not current:
            return
        state = conn.execute('SELECT * FROM retailer_state WHERE id=1').fetchone()
        if state['blocked_until'] > now:
            conn.execute("UPDATE alerts SET status='blocked',detail=?,next_check=MAX(next_check,?) WHERE id=?",
                         (state['message'], state['blocked_until'], alert['id']))
            return
        # Re-read current configuration after a queued alert may have been edited.
        alert = dict(current)
        conn.execute("UPDATE alerts SET status='checking' WHERE id=?", (alert['id'],))
        conn.execute('UPDATE retailer_state SET last_request=? WHERE id=1', (now,))
        conn.execute('INSERT OR REPLACE INTO worker_health(id,heartbeat) VALUES(1,?)', (now,))
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
        with db.connect() as conn:
            delay = interval
            if status == 'blocked':
                state = conn.execute('SELECT block_streak FROM retailer_state WHERE id=1').fetchone()
                streak = state['block_streak'] + 1
                delay = max(interval, min(900 * 2 ** min(streak - 1, 4), 14400), getattr(exc, 'retry_after', 0))
                message = f'{message} All alerts are cooling down for {int(delay // 60)} minutes; no stock result was recorded.'
                conn.execute('UPDATE retailer_state SET blocked_until=?,block_streak=?,message=? WHERE id=1',
                             (now + delay, streak, message))
                # A block affects this retailer/session, not one product. Do not
                # immediately request every other alert or lose the cooldown on restart.
                conn.execute("UPDATE alerts SET status='blocked',detail=?,next_check=MAX(next_check,?) WHERE enabled=1",
                             (message, now + delay))
            conn.execute('UPDATE alerts SET status=?,detail=?,last_checked=?,next_check=? WHERE id=? AND enabled=1',
                         (status, message, now, now + delay if status == 'blocked' or next_run is None else db.schedule()['next_run'], alert['id']))
            if conn.execute('SELECT 1 FROM alerts WHERE id=?', (alert['id'],)).fetchone():
                db.event(conn, alert['id'], status, message, now)
        return True
    now = clock()
    settings = db.settings()
    status = 'available' if result.available else 'unavailable'
    with db.connect() as conn:
        conn.execute("UPDATE retailer_state SET blocked_until=0,block_streak=0,message='' WHERE id=1")
        # UI may have deleted or paused the alert while Chrome was checking.
        current = conn.execute('SELECT * FROM alerts WHERE id=? AND enabled=1', (alert['id'],)).fetchone()
        if not current:
            return
        following = db.schedule()['next_run'] if next_run is not None else now + settings['interval']
        conn.execute('UPDATE alerts SET title=?,status=?,detail=?,quantity=?,price=?,last_checked=?,next_check=?,last_transport=? WHERE id=?',
                     (result.title, status, result.detail, result.quantity, result.price, now,
                      following, result.transport, alert['id']))
        db.event(conn, alert['id'], status, result.detail, now)
        if not result.available:
            conn.execute('UPDATE alerts SET last_sent_signature=NULL,notification_error=NULL WHERE id=?', (alert['id'],))
    if result.available and current['last_sent_signature'] != result.signature:
        if not settings['enabled']:
            with db.connect() as conn:
                conn.execute('UPDATE alerts SET notification_error=? WHERE id=?',
                             ('Stock found. Enable global email settings to receive notifications.', alert['id']))
            return True
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
    return True


def run_batch(db, factory=BatchChecker, sender=notify, clock=time.time, should_stop=lambda: False):
    batch = claim_batch(db, clock())
    if batch is None:
        return None
    completed = 0
    http_requests = 0
    browser_requests = 0
    summary = 'Completed'
    processed = set()
    last_request_started = None
    by_store = {}
    for alert in batch.alerts:
        by_store.setdefault(alert['store_id'], []).append(alert)
    try:
        for store_id, alerts in by_store.items():
            if should_stop():
                summary = 'Interrupted; remaining alerts queued'
                break
            if db.retailer()['blocked_until'] > clock():
                summary = 'Paused by retailer; remaining alerts deferred together'
                break
            # Keep each store's alerts in its own session/profile. Maintain the
            # same fixed 15-second floor between navigations across store sessions.
            if last_request_started is not None:
                time.sleep(max(0, 15 - (clock() - last_request_started)))
            checker = factory(db.directory / 'browser-profile' / store_id,
                              transport=db.settings()['transport'])
            try:
                for alert in alerts:
                    if should_stop():
                        summary = 'Interrupted; remaining alerts queued'
                        break
                    if db.retailer()['blocked_until'] > clock():
                        summary = 'Paused by retailer; remaining alerts deferred together'
                        break
                    if run_check(db, alert, checker, sender, clock, next_run=batch.next_run):
                        completed += 1
                    processed.add(alert['id'])
                    last_request_started = db.retailer()['last_request']
            finally:
                http_requests += checker.http_requests
                browser_requests += checker.browser_requests
                checker.close()
            if summary != 'Completed':
                break
    except Exception:
        summary = 'Interrupted; remaining alerts queued'
        log.exception('Batch %s failed', batch.id)
    finally:
        if summary.startswith('Interrupted'):
            with db.connect() as conn:
                for alert in batch.alerts:
                    if alert['id'] not in processed:
                        conn.execute("UPDATE alerts SET check_requested=1,request_reason='recovery' WHERE id=? AND enabled=1", (alert['id'],))
        finish_batch(db, batch, clock(), completed, http_requests, browser_requests, summary)
    log.info('Batch %s (%s): %s/%s alerts; HTTP=%s browser=%s; %s', batch.id, batch.kind,
             completed, len(batch.alerts), http_requests, browser_requests, summary)
    return batch


def main():
    logging.basicConfig(level=logging.INFO)
    db = Database()
    # Only one polling worker can own this data directory.
    lock = open(db.directory / 'worker.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # Restart recovery: a killed browser check must not remain "checking" forever.
    with db.connect() as conn:
        conn.execute("UPDATE alerts SET status='pending',check_requested=1,request_reason='recovery' WHERE enabled=1 AND (status='checking' OR last_batch_id IN (SELECT id FROM batch_runs WHERE finished_at IS NULL))")
        conn.execute("UPDATE batch_runs SET finished_at=?,summary='Interrupted by restart; alerts queued' WHERE finished_at IS NULL", (time.time(),))
    while running:
        with db.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO worker_health(id,heartbeat) VALUES(1,?)', (time.time(),))
        if run_batch(db, should_stop=lambda: not running) is None:
            time.sleep(2)


if __name__ == '__main__':
    main()

"""One anchored cadence; immediate requests never move its next tick."""
from dataclasses import dataclass


def next_tick(now, interval):
    return (int(now) // interval + 1) * interval


@dataclass
class Batch:
    id: int
    kind: str
    alerts: list
    next_run: float


def claim_batch(db, now, transport='http'):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        state = conn.execute('SELECT * FROM batch_schedule WHERE id=1').fetchone()
        interval, next_run = state['interval'], state['next_run']
        if not next_run:
            next_run = next_tick(now, interval)
            conn.execute('UPDATE batch_schedule SET next_run=? WHERE id=1', (next_run,))
            conn.execute('UPDATE alerts SET next_check=? WHERE enabled=1', (next_run,))
        blocked = conn.execute('SELECT blocked_until,last_request FROM retailer_state WHERE id=1').fetchone()
        if transport != 'apify' and max(blocked['blocked_until'], blocked['last_request'] + 15) > now:
            return None
        scheduled = now >= next_run
        if scheduled:
            # Coalesce missed ticks into one batch, including after a restart.
            next_run = next_tick(now, interval)
            conn.execute('UPDATE batch_schedule SET next_run=? WHERE id=1', (next_run,))
            rows = conn.execute('SELECT * FROM alerts WHERE enabled=1 ORDER BY store_id,product_id,id').fetchall()
            kind = 'scheduled'
        else:
            rows = conn.execute('SELECT * FROM alerts WHERE enabled=1 AND check_requested=1 ORDER BY store_id,product_id,id').fetchall()
            kind = 'requested'
        if not rows:
            return None
        cursor = conn.execute('INSERT INTO batch_runs(kind,started_at,alert_count) VALUES(?,?,?)', (kind, now, len(rows)))
        batch_id = cursor.lastrowid
        for row in rows:
            conn.execute('UPDATE alerts SET check_requested=0,next_check=?,last_batch_id=? WHERE id=?',
                         (next_run, batch_id, row['id']))
        return Batch(batch_id, kind, [dict(row) for row in rows], next_run)


def finish_batch(db, batch, now, completed, http_requests, browser_requests, summary):
    with db.connect() as conn:
        conn.execute('UPDATE batch_runs SET finished_at=?,completed=?,http_requests=?,browser_requests=?,summary=? WHERE id=?',
                     (now, completed, http_requests, browser_requests, summary, batch.id))
        # A long batch must not immediately replay ticks it overlapped. Keep the
        # global clock aligned, also when the user changed its interval mid-batch.
        state = conn.execute('SELECT * FROM batch_schedule WHERE id=1').fetchone()
        following = state['next_run']
        if following <= now:
            following = next_tick(now, state['interval'])
            conn.execute('UPDATE batch_schedule SET next_run=? WHERE id=1', (following,))
        conn.execute('UPDATE alerts SET next_check=MAX(?,(SELECT blocked_until FROM retailer_state WHERE id=1)) WHERE enabled=1', (following,))
        conn.execute('DELETE FROM batch_runs WHERE id NOT IN (SELECT id FROM batch_runs ORDER BY id DESC LIMIT 100)')

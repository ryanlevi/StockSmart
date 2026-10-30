import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

DEFAULT_SETTINGS = {
    'enabled': False, 'host': 'smtp.gmail.com', 'port': 587,
    'security': 'starttls', 'username': '', 'sender': '', 'recipient': '',
    'password': '', 'interval': 300, 'transport': 'http',
}


class Database:
    def __init__(self, directory=None):
        self.directory = Path(directory or os.environ.get('STOCKSMART_DATA', 'data')).resolve()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        keyfile = self.directory / 'secret.key'
        # Publish a complete key atomically; UI and worker may start together.
        fd, temporary = tempfile.mkstemp(prefix='.key-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
            try:
                os.link(temporary, keyfile)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
        self.key = keyfile.read_bytes()
        self.cipher = Fernet(self.key)
        self.path = self.directory / 'stocksmart.sqlite3'
        with self.connect() as conn:
            conn.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL, product_id TEXT NOT NULL, title TEXT NOT NULL,
                    store_id TEXT NOT NULL, open_box INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'pending',
                    detail TEXT NOT NULL DEFAULT 'Waiting for the first check.',
                    quantity TEXT, price TEXT, last_checked REAL, next_check REAL NOT NULL DEFAULT 0,
                    last_sent_signature TEXT, last_notified REAL, notification_error TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(product_id, store_id, open_box)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_health (
                    id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS retailer_state (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    blocked_until REAL NOT NULL DEFAULT 0,
                    block_streak INTEGER NOT NULL DEFAULT 0,
                    last_request REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT ''
                );
                INSERT OR IGNORE INTO retailer_state(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS batch_schedule (
                    id INTEGER PRIMARY KEY CHECK(id=1), interval INTEGER NOT NULL DEFAULT 300,
                    next_run REAL NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO batch_schedule(id) VALUES(1);
                CREATE TABLE IF NOT EXISTS batch_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL, alert_count INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0, http_requests INTEGER NOT NULL DEFAULT 0,
                    browser_requests INTEGER NOT NULL DEFAULT 0, summary TEXT NOT NULL DEFAULT 'Running'
                );
            ''')
            conn.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(alerts)')}
            for name, definition in {
                'check_requested': 'INTEGER NOT NULL DEFAULT 0',
                'request_reason': "TEXT NOT NULL DEFAULT 'first'",
                'last_transport': 'TEXT', 'last_batch_id': 'INTEGER',
            }.items():
                if name not in columns:
                    conn.execute(f'ALTER TABLE alerts ADD COLUMN {name} {definition}')
            if 'check_requested' not in columns:
                conn.execute("UPDATE alerts SET check_requested=1 WHERE last_checked IS NULL OR status='pending'")
            settings = conn.execute('SELECT payload FROM settings WHERE id=1').fetchone()
            interval = json.loads(settings['payload']).get('interval', 300) if settings else 300
            conn.execute('UPDATE batch_schedule SET interval=? WHERE next_run=0', (interval,))
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def settings(self):
        with self.connect() as conn:
            row = conn.execute('SELECT payload FROM settings WHERE id=1').fetchone()
        result = dict(DEFAULT_SETTINGS)
        if row:
            result.update(json.loads(row['payload']))
        if result['password']:
            result['password'] = self.cipher.decrypt(result['password'].encode()).decode()
        return result

    def save_settings(self, settings):
        payload = dict(settings)
        if payload['password']:
            payload['password'] = self.cipher.encrypt(payload['password'].encode()).decode()
        with self.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO settings(id,payload) VALUES (1,?)', (json.dumps(payload),))
            schedule = conn.execute('SELECT interval FROM batch_schedule WHERE id=1').fetchone()
            if schedule['interval'] != payload['interval']:
                interval = payload['interval']
                next_run = (int(time.time()) // interval + 1) * interval
                conn.execute('UPDATE batch_schedule SET interval=?,next_run=? WHERE id=1', (interval, next_run))
                conn.execute('UPDATE alerts SET next_check=? WHERE enabled=1', (next_run,))

    def alerts(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM alerts ORDER BY created_at DESC')]

    def alert(self, alert_id):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM alerts WHERE id=?', (alert_id,)).fetchone()
            return dict(row) if row else None

    def retailer(self):
        with self.connect() as conn:
            return dict(conn.execute('SELECT * FROM retailer_state WHERE id=1').fetchone())

    def schedule(self):
        with self.connect() as conn:
            return dict(conn.execute('SELECT * FROM batch_schedule WHERE id=1').fetchone())

    def latest_batch(self):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM batch_runs ORDER BY id DESC LIMIT 1').fetchone()
            return dict(row) if row else None

    @staticmethod
    def event(conn, alert_id, status, message, now):
        conn.execute('INSERT INTO events(alert_id,created_at,status,message) VALUES (?,?,?,?)',
                     (alert_id, now, status, message))
        conn.execute('DELETE FROM events WHERE alert_id=? AND id NOT IN '
                     '(SELECT id FROM events WHERE alert_id=? ORDER BY id DESC LIMIT 100)',
                     (alert_id, alert_id))

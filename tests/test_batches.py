import tempfile
import time
import unittest
from email.utils import formatdate
from unittest.mock import Mock, patch

import requests

from stocksmart.db import Database, DEFAULT_SETTINGS
from stocksmart.inventory import CheckFailure, Inventory
from stocksmart.scheduler import claim_batch, finish_batch, next_tick
from stocksmart.sources import BatchChecker, retry_after_seconds
from stocksmart.worker import run_batch

URL = 'https://www.microcenter.com/product/685416/monitor'
HTML = '<html><title>Monitor</title><h1>Monitor</h1><p>11 NEW IN STOCK at Tustin Store</p><p>1 Open Box: from $99.00 ID: 123</p><p>Specifications Section</p></html>'


class Clock:
    def __init__(self, now=1700000000):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Response:
    def __init__(self, status=200, body=HTML, headers=None):
        self.status_code = status
        self.body = body
        self.headers = {'Content-Type': 'text/html', **(headers or {})}
        self.encoding = 'utf-8'
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body.encode()

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses=None):
        self.headers = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.responses = list(responses or [Response()])
        self.urls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def close(self):
        self.closed = True


def alert(product='685416', box=False, store='101'):
    return {'product_id': product, 'url': URL.replace('685416', product), 'store_id': store, 'open_box': box}


class HTTPTests(unittest.TestCase):
    def checker(self, session, mode='http'):
        clock = Clock()
        browser = Mock(side_effect=AssertionError('Browser must not launch'))
        checker = BatchChecker(transport=mode, session_factory=lambda: session,
                               browser_factory=browser, clock=clock.time, sleep=clock.sleep)
        return checker, browser

    def test_http_only_uses_one_fetch_for_both_conditions_and_no_browser(self):
        session = Session()
        checker, browser = self.checker(session)
        self.assertTrue(checker.check(alert()).available)
        self.assertTrue(checker.check(alert(box=True)).available)
        self.assertEqual(checker.http_requests, 1)
        self.assertEqual(checker.browser_requests, 0)
        self.assertEqual(session.cookies.get('storeSelected'), '101')
        self.assertEqual(checker.check(alert()).transport, 'http')
        checker.close()
        self.assertTrue(session.closed)
        browser.assert_not_called()

    def test_denial_never_escalates_to_browser_even_in_auto_mode(self):
        for status in [401, 403, 429]:
            with self.subTest(status=status):
                session = Session([Response(status=status, headers={'Retry-After': '3600'})])
                checker, browser = self.checker(session, 'auto')
                with self.assertRaises(CheckFailure) as caught:
                    checker.check(alert())
                self.assertEqual(caught.exception.status, 'blocked')
                self.assertEqual(caught.exception.retry_after, 3600)
                browser.assert_not_called()
                self.assertEqual(checker.browser_requests, 0)
                checker.close()

    def test_challenge_header_on_http_200_is_not_inventory(self):
        checker, browser = self.checker(Session([Response(headers={'cf-mitigated': 'challenge'})]), 'auto')
        with self.assertRaises(CheckFailure) as caught:
            checker.check(alert())
        self.assertEqual(caught.exception.status, 'blocked')
        browser.assert_not_called()

    def test_challenge_body_on_http_200_is_not_inventory(self):
        checker, browser = self.checker(Session([Response(body='<title>Just a moment...</title>')]), 'auto')
        with self.assertRaises(CheckFailure) as caught:
            checker.check(alert())
        self.assertEqual(caught.exception.status, 'blocked')
        browser.assert_not_called()

    def test_incomplete_html_stays_browser_free_in_http_only(self):
        checker, browser = self.checker(Session([Response(body='<h1>Monitor</h1>')]))
        with self.assertRaises(CheckFailure):
            checker.check(alert())
        browser.assert_not_called()

    def test_auto_can_render_incomplete_success_with_one_browser(self):
        clock = Clock()
        session = Session([Response(body='<h1>Monitor</h1>')])
        browser = Mock()
        browser.cache = {}
        browser.browser_requests = 1
        browser.check.return_value = Inventory('Monitor', True, '1', None, 'Found', 'new')
        factory = Mock(return_value=browser)
        checker = BatchChecker(transport='auto', session_factory=lambda: session,
                               browser_factory=factory, clock=clock.time, sleep=clock.sleep)
        self.assertEqual(checker.check(alert()).transport, 'browser')
        self.assertEqual(checker.http_requests, 1)
        self.assertEqual(checker.browser_requests, 1)
        factory.assert_called_once()
        checker.close()
        browser.close.assert_called_once()

    def test_cross_origin_redirect_is_rejected_without_fetching_target(self):
        session = Session([Response(302, headers={'Location': 'http://169.254.169.254/latest/meta-data/'})])
        checker, _ = self.checker(session)
        with self.assertRaises(CheckFailure):
            checker.check(alert())
        self.assertEqual(len(session.urls), 1)

    def test_wrong_product_redirect_is_rejected(self):
        session = Session([Response(302, headers={'Location': '/product/999999/other'})])
        checker, _ = self.checker(session)
        with self.assertRaises(CheckFailure):
            checker.check(alert())
        self.assertEqual(len(session.urls), 1)

    def test_repeated_parse_failure_is_not_refetched_in_one_batch(self):
        session = Session([Response(body='<h1>Monitor</h1>')])
        checker, _ = self.checker(session)
        for box in [False, True]:
            with self.assertRaises(CheckFailure):
                checker.check(alert(box=box))
        self.assertEqual(checker.http_requests, 1)

    def test_retry_after_numeric_and_http_date(self):
        self.assertEqual(retry_after_seconds('120'), 120)
        self.assertEqual(retry_after_seconds(formatdate(1700000300, usegmt=True), now=1700000000), 300)
        for value in ['NaN', 'Infinity', 'not-a-date', '-10']:
            self.assertEqual(retry_after_seconds(value), 0)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(self.temp.name)
        self.clock = Clock()

    def tearDown(self):
        self.temp.cleanup()

    def add(self, product='685416', box=False, requested=True):
        with self.db.connect() as conn:
            row = conn.execute('INSERT INTO alerts(url,product_id,title,store_id,open_box,created_at,check_requested) VALUES(?,?,?,?,?,?,?)',
                               (URL.replace('685416', product), product, 'Monitor', '101', int(box), self.clock.now, int(requested)))
            return row.lastrowid

    def set_request(self, alert_id):
        with self.db.connect() as conn:
            conn.execute("UPDATE alerts SET check_requested=1,request_reason='manual' WHERE id=?", (alert_id,))

    def test_first_and_manual_requests_do_not_shift_cadence(self):
        one = self.add()
        first = claim_batch(self.db, self.clock.now)
        tick = next_tick(self.clock.now, 300)
        self.assertEqual(first.kind, 'requested')
        self.assertEqual([a['id'] for a in first.alerts], [one])
        finish_batch(self.db, first, self.clock.now, 1, 1, 0, 'Completed')
        two = self.add('685417')
        first_two = claim_batch(self.db, self.clock.now + 20)
        self.assertEqual([a['id'] for a in first_two.alerts], [two])
        self.assertEqual(self.db.schedule()['next_run'], tick)
        self.set_request(one)
        manual = claim_batch(self.db, self.clock.now + 40)
        self.assertEqual([a['id'] for a in manual.alerts], [one])
        self.assertEqual(self.db.schedule()['next_run'], tick)
        scheduled = claim_batch(self.db, tick)
        self.assertEqual(scheduled.kind, 'scheduled')
        self.assertEqual({a['id'] for a in scheduled.alerts}, {one, two})
        self.assertEqual(self.db.schedule()['next_run'], tick + 300)

    def test_scheduled_batch_ignores_old_per_alert_drift(self):
        self.add(requested=False)
        self.add('685417', requested=False)
        claim_batch(self.db, self.clock.now)
        tick = self.db.schedule()['next_run']
        with self.db.connect() as conn:
            conn.execute('UPDATE alerts SET next_check=? WHERE id=1', (tick+9999,))
            conn.execute('UPDATE alerts SET next_check=? WHERE id=2', (tick-10,))
        batch = claim_batch(self.db, tick)
        self.assertEqual(len(batch.alerts), 2)
        self.assertEqual(len({a['next_check'] for a in self.db.alerts()}), 1)

    def test_restart_keeps_tick_and_missed_ticks_coalesce(self):
        self.add(requested=False)
        claim_batch(self.db, self.clock.now)
        tick = self.db.schedule()['next_run']
        reopened = Database(self.temp.name)
        self.assertEqual(reopened.schedule()['next_run'], tick)
        batch = claim_batch(reopened, tick+901)
        self.assertEqual(len(batch.alerts), 1)
        future = reopened.schedule()['next_run']
        self.assertGreater(future, tick+901)
        self.assertIsNone(claim_batch(reopened, tick+901))

    def test_long_batch_skips_overlap_without_catchup_loop(self):
        self.add()
        batch = claim_batch(self.db, self.clock.now)
        finished = self.clock.now+700
        finish_batch(self.db, batch, finished, 1, 1, 0, 'Completed')
        self.assertEqual(self.db.schedule()['next_run'], next_tick(finished, 300))
        self.assertIsNone(claim_batch(self.db, finished))

    def test_interval_change_mid_batch_stays_on_new_shared_clock(self):
        self.add()
        batch = claim_batch(self.db, self.clock.now)
        with patch('stocksmart.db.time.time', return_value=self.clock.now+10):
            self.db.save_settings(dict(DEFAULT_SETTINGS, interval=600))
        expected = next_tick(self.clock.now+10, 600)
        finish_batch(self.db, batch, self.clock.now+20, 1, 1, 0, 'Completed')
        self.assertEqual(self.db.schedule()['next_run'], expected)
        self.assertEqual(self.db.alert(1)['next_check'], expected)

    def test_cooldown_does_not_discard_pending_manual_requests(self):
        one = self.add()
        with self.db.connect() as conn:
            conn.execute('UPDATE retailer_state SET blocked_until=? WHERE id=1', (self.clock.now+600,))
        self.assertIsNone(claim_batch(self.db, self.clock.now))
        self.assertEqual(self.db.alert(one)['check_requested'], 1)
        self.assertIsNotNone(claim_batch(self.db, self.clock.now+601))

    def test_one_session_and_two_fetches_for_three_alerts(self):
        self.add()
        self.add(box=True)
        self.add('685417')
        session = Session([Response(), Response()])
        factory = Mock(side_effect=lambda profile, transport: BatchChecker(profile, transport,
                       session_factory=lambda: session, clock=self.clock.time, sleep=self.clock.sleep))
        batch = run_batch(self.db, factory=factory, clock=self.clock.time)
        self.assertIsNotNone(batch)
        factory.assert_called_once()
        self.assertEqual(len(session.urls), 2)
        self.assertTrue(session.closed)
        result = self.db.latest_batch()
        self.assertEqual(result['completed'], 3)
        self.assertEqual(result['http_requests'], 2)
        self.assertEqual(result['browser_requests'], 0)
        self.assertTrue(all(a['last_batch_id'] == batch.id for a in self.db.alerts()))
        self.assertTrue(all(a['last_transport'] == 'http' for a in self.db.alerts()))
        self.assertEqual(len({a['next_check'] for a in self.db.alerts()}), 1)

    def test_http_denial_stops_batch_and_honors_retry_after(self):
        self.add()
        self.add('685417')
        session = Session([Response(429, headers={'Retry-After': '7200'})])
        factory = lambda profile, transport: BatchChecker(profile, transport, session_factory=lambda: session,
                                                          clock=self.clock.time, sleep=self.clock.sleep)
        run_batch(self.db, factory=factory, clock=self.clock.time)
        self.assertEqual(len(session.urls), 1)
        self.assertEqual(self.db.latest_batch()['http_requests'], 1)
        self.assertEqual(self.db.latest_batch()['browser_requests'], 0)
        self.assertEqual(self.db.retailer()['blocked_until'], self.clock.now+7200)
        self.assertEqual(self.db.alert(2)['last_checked'], None)
        self.assertTrue(session.closed)

    def test_interrupted_batch_requeues_unprocessed_alerts(self):
        one = self.add()
        session = Session()
        factory = lambda profile, transport: BatchChecker(profile, transport, session_factory=lambda: session)
        run_batch(self.db, factory=factory, clock=self.clock.time, should_stop=lambda: True)
        self.assertEqual(self.db.alert(one)['check_requested'], 1)
        self.assertEqual(self.db.latest_batch()['completed'], 0)
        self.assertTrue(session.closed)



class QueueWebTests(unittest.TestCase):
    def setUp(self):
        from stocksmart import create_app
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name)
        self.app.testing = True
        self.client = self.app.test_client()
        self.db = self.app.extensions['stocksmart_db']
        self.client.get('/')
        with self.client.session_transaction() as state:
            self.csrf = state['csrf']

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, values=None):
        return self.client.post(path, data={'csrf_token': self.csrf, **(values or {})})

    def test_first_check_is_explicitly_queued(self):
        self.post('/alerts/new', {'url': URL, 'store_id': '101'})
        self.assertEqual(self.db.alert(1)['check_requested'], 1)
        self.assertEqual(self.db.alert(1)['request_reason'], 'first')

    def test_manual_check_queues_only_target_without_moving_schedule(self):
        self.post('/alerts/new', {'url': URL, 'store_id': '101'})
        tick = next_tick(time.time(), 300)
        with self.db.connect() as conn:
            conn.execute('UPDATE batch_schedule SET next_run=? WHERE id=1', (tick,))
            conn.execute("UPDATE alerts SET check_requested=0,status='unavailable',last_checked=? WHERE id=1", (time.time()-120,))
        self.post('/alerts/1/check')
        self.assertEqual(self.db.alert(1)['check_requested'], 1)
        self.assertEqual(self.db.alert(1)['request_reason'], 'manual')
        self.assertEqual(self.db.schedule()['next_run'], tick)

    def test_source_is_configurable_and_invalid_method_rejected(self):
        values = {'host': 'smtp.gmail.com', 'port': '587', 'security': 'starttls', 'interval': '300', 'transport': 'auto'}
        self.assertEqual(self.post('/settings', values).status_code, 302)
        self.assertEqual(self.db.settings()['transport'], 'auto')
        values['transport'] = 'unsupported'
        self.assertEqual(self.post('/settings', values).status_code, 400)
        self.assertEqual(self.db.settings()['transport'], 'auto')


if __name__ == '__main__':
    unittest.main()

import re
import smtplib
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from stocksmart import create_app
from stocksmart.db import Database, DEFAULT_SETTINGS
from stocksmart.inventory import BrowserChecker, CheckFailure, Inventory, parse_inventory, product_url, validate_url
from stocksmart.mail import send_message
from stocksmart.stores import STORES
from stocksmart.worker import run_check

URL = 'https://www.microcenter.com/product/685416/alienware-monitor'


def page(stock='11 NEW IN STOCK at Tustin Store', openbox='', extra=''):
    # Minimal purchase-region fixtures using Micro Center's observed stock wording.
    return f'<html><title>Monitor</title><nav>Open Box</nav><h1>Alienware Monitor</h1><p>{stock}</p><div>{openbox}</div><p>Specifications Section</p>{extra}</html>'


class InventoryTests(unittest.TestCase):
    def test_store_map_and_leading_zeroes(self):
        self.assertEqual(len(STORES), 31)
        self.assertEqual(STORES['101'], 'CA — Tustin')
        self.assertEqual(STORES['025'], 'IL — Westmont')
        self.assertEqual(STORES['081'], 'VA — Fairfax')

    def test_regular_stock_never_triggers_openbox(self):
        html = page()
        self.assertTrue(parse_inventory(html, '101').available)
        self.assertFalse(parse_inventory(html, '101', True).available)

    def test_openbox_available_even_when_new_is_sold_out(self):
        html = page('0 NEW IN STOCK at Tustin Store', '2 Open Box: from $692.96 ID: 417859 ID: 417860')
        self.assertFalse(parse_inventory(html, '101').available)
        result = parse_inventory(html, '101', True)
        self.assertTrue(result.available)
        self.assertEqual(result.quantity, '2')
        self.assertEqual(result.price, '$692.96')

    def test_new_openbox_listing_changes_signature(self):
        a = parse_inventory(page(openbox='1 Open Box: from $692.96 ID: 417859'), '101', True)
        b = parse_inventory(page(openbox='1 Open Box: from $692.96 ID: 417860'), '101', True)
        self.assertNotEqual(a.signature, b.signature)

    def test_openbox_count_without_price(self):
        result = parse_inventory(page(openbox='1 Open Box'), '101', True)
        self.assertTrue(result.available)
        self.assertIsNone(result.price)

    def test_other_store_is_not_evidence(self):
        with self.assertRaises(CheckFailure):
            parse_inventory(page('11 NEW IN STOCK at Dallas Store'), '101', True)

    def test_other_products_and_nav_are_not_evidence(self):
        html = page(extra='<h2>Related Products</h2>22 NEW IN STOCK at Tustin Store 3 Open Box: from $5.00')
        self.assertFalse(parse_inventory(html, '101', True).available)
        with self.assertRaises(CheckFailure):
            parse_inventory(page(stock='', extra='2 NEW IN STOCK at Tustin Store'), '101')

    def test_sold_out(self):
        self.assertFalse(parse_inventory(page('SOLD OUT at Tustin Store'), '101').available)

    def test_security_page_is_blocked(self):
        with self.assertRaises(CheckFailure) as caught:
            parse_inventory('<title>Just a moment...</title><h1>Security verification</h1>', '101')
        self.assertEqual(caught.exception.status, 'blocked')

    def test_unrecognized_page_is_unknown(self):
        with self.assertRaises(CheckFailure):
            parse_inventory('<h1>Product</h1><p>Check back soon</p>', '101')

    def test_url_canonicalization_and_store_override(self):
        clean, product_id = validate_url(URL + '?storeid=131&ob=1#reviews')
        self.assertEqual(clean, URL)
        self.assertEqual(product_id, '685416')
        self.assertEqual(product_url(clean, '025', True), URL + '?storeid=025&ob=1')

    def test_invalid_urls(self):
        for url in ['http://www.microcenter.com/product/685416/x', 'https://evil.com/product/685416/x',
                    'https://www.microcenter.com.evil.com/product/685416/x',
                    'https://user@www.microcenter.com/product/685416/x',
                    'https://www.microcenter.com:8080/product/685416/x',
                    'https://www.microcenter.com/search/search_results.aspx', 'file:///etc/passwd']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name)
        self.app.testing = True
        self.client = self.app.test_client()
        self.db = self.app.extensions['stocksmart_db']
        self.client.get('/')
        with self.client.session_transaction() as session:
            self.csrf = session['csrf']

    def tearDown(self):
        self.temp.cleanup()

    def post(self, url, values=None):
        return self.client.post(url, data={'csrf_token': self.csrf, **(values or {})})

    def create(self, open_box=False):
        values = {'store_id': '101', 'url': URL}
        if open_box:
            values['open_box'] = 'on'
        return self.post('/alerts/new', values)

    def test_empty_and_form_render(self):
        self.assertIn(b'Set up a new stock alert', self.client.get('/').data)
        self.assertIn(b'CA \xe2\x80\x94 Tustin', self.client.get('/alerts/new').data)
        self.assertEqual(self.client.get('/settings').status_code, 200)

    def test_create_duplicate_and_distinct_stock_types(self):
        self.assertEqual(self.create().status_code, 302)
        self.assertEqual(self.create().status_code, 400)
        self.assertEqual(self.create(True).status_code, 302)
        self.assertEqual(len(self.db.alerts()), 2)
        self.assertIn(b'New stock', self.client.get('/').data)
        self.assertIn(b'Open box', self.client.get('/?filter=openbox').data)

    def test_invalid_store_and_url_do_not_save(self):
        self.assertEqual(self.post('/alerts/new', {'store_id': '999', 'url': URL}).status_code, 400)
        self.assertEqual(self.post('/alerts/new', {'store_id': '101', 'url': 'https://evil.com'}).status_code, 400)
        self.assertEqual(self.db.alerts(), [])

    def test_csrf_required(self):
        self.assertEqual(self.client.post('/alerts/new', data={'url': URL, 'store_id': '101'}).status_code, 400)
        self.assertEqual(self.client.post('/settings').status_code, 400)

    def test_edit_pause_resume_and_delete(self):
        self.create()
        self.assertEqual(self.client.get('/alerts/1/edit').status_code, 200)
        self.post('/alerts/1/edit', {'url': URL, 'store_id': '025', 'open_box': 'on'})
        self.assertEqual(self.db.alert(1)['store_id'], '025')
        self.assertEqual(self.db.alert(1)['open_box'], 1)
        self.post('/alerts/1/toggle')
        self.assertEqual(self.db.alert(1)['enabled'], 0)
        self.post('/alerts/1/toggle')
        self.assertEqual(self.db.alert(1)['enabled'], 1)
        self.post('/alerts/1/delete')
        self.assertIsNone(self.db.alert(1))

    def test_settings_password_encrypted_and_never_rendered(self):
        settings = {'enabled': 'on', 'host': 'smtp.gmail.com', 'port': '587', 'security': 'starttls',
                    'username': 'me@example.com', 'sender': 'me@example.com', 'recipient': 'me@example.com',
                    'password': 'super-secret-app-password', 'interval': '300'}
        self.assertEqual(self.post('/settings', settings).status_code, 302)
        self.assertEqual(self.db.settings()['password'], settings['password'])
        with self.db.connect() as conn:
            payload = conn.execute('SELECT payload FROM settings').fetchone()[0]
        self.assertNotIn(settings['password'], payload)
        self.assertNotIn(settings['password'].encode(), self.client.get('/settings').data)
        settings['password'] = ''
        self.post('/settings', settings)
        self.assertEqual(self.db.settings()['password'], 'super-secret-app-password')
        settings['clear_password'] = 'on'
        self.post('/settings', settings)
        self.assertEqual(self.db.settings()['password'], '')

    def test_settings_test_email_and_validation(self):
        settings = {'enabled': 'on', 'host': 'smtp.example.com', 'port': '465', 'security': 'ssl',
                    'sender': 'me@example.com', 'recipient': 'you@example.com', 'interval': '600', 'action': 'test'}
        with patch('stocksmart.send_message') as send:
            self.assertEqual(self.post('/settings', settings).status_code, 302)
            send.assert_called_once()
        settings['port'] = 'not-a-port'
        self.assertEqual(self.post('/settings', settings).status_code, 400)
        settings['port'] = '465'
        settings['sender'] = 'me@example.com\nBCC:other@example.com'
        self.assertEqual(self.post('/settings', settings).status_code, 400)

    def test_persistence_and_history(self):
        self.create()
        self.assertEqual(len(Database(self.temp.name).alerts()), 1)
        self.assertEqual(self.client.get('/alerts/1/history').status_code, 200)
        self.assertEqual(self.client.get('/alerts/999/history').status_code, 404)

    def test_worker_dedup_retry_and_availability_transitions(self):
        self.create(True)
        settings = dict(DEFAULT_SETTINGS, enabled=True, sender='me@example.com', recipient='me@example.com')
        self.db.save_settings(settings)
        now = [time.time()]
        sender = Mock()
        checker = Mock()
        checker.check.return_value = Inventory('Monitor', True, '1', '$200.00', '1 open box', 'listing-a')
        run_check(self.db, self.db.alert(1), checker, sender, clock=lambda: now[0])
        self.assertEqual(sender.call_count, 1)
        run_check(self.db, self.db.alert(1), checker, sender, clock=lambda: now[0])
        self.assertEqual(sender.call_count, 1)
        checker.check.side_effect = CheckFailure('Verification', 'blocked')
        run_check(self.db, self.db.alert(1), checker, sender, clock=lambda: now[0])
        self.assertEqual(self.db.alert(1)['status'], 'blocked')
        self.assertEqual(self.db.alert(1)['last_sent_signature'], 'listing-a')
        self.assertGreater(self.db.alert(1)['next_check'] - time.time(), 895)
        now[0] = self.db.retailer()['blocked_until'] + 1
        checker.check.side_effect = None
        run_check(self.db, self.db.alert(1), checker, sender, clock=lambda: now[0])
        self.assertEqual(sender.call_count, 1)
        checker.check.return_value = Inventory('Monitor', False, '0', None, '0 open box', 'empty')
        run_check(self.db, self.db.alert(1), checker, sender, clock=lambda: now[0])
        self.assertIsNone(self.db.alert(1)['last_sent_signature'])
        checker.check.return_value = Inventory('Monitor', True, '1', '$200.00', '1 open box', 'listing-b')
        sender.side_effect = smtplib.SMTPAuthenticationError(535, b'secret provider details')
        run_check(self.db, self.db.alert(1), checker, sender, clock=lambda: now[0])
        self.assertIsNone(self.db.alert(1)['last_sent_signature'])
        self.assertNotIn('secret', self.db.alert(1)['notification_error'])
        sender.side_effect = None
        run_check(self.db, self.db.alert(1), checker, sender, clock=lambda: now[0])
        self.assertEqual(self.db.alert(1)['last_sent_signature'], 'listing-b')
        self.assertIsNone(self.db.alert(1)['notification_error'])

    def test_disabled_email_does_not_consume_notification(self):
        self.create()
        checker = Mock()
        checker.check.return_value = Inventory('Monitor', True, '1', None, 'Found', 'new-in-stock')
        sender = Mock()
        run_check(self.db, self.db.alert(1), checker, sender)
        sender.assert_not_called()
        self.assertIsNone(self.db.alert(1)['last_sent_signature'])

    def test_paused_or_deleted_during_check_does_not_send(self):
        self.create()
        self.db.save_settings(dict(DEFAULT_SETTINGS, enabled=True))
        def pause(alert):
            with self.db.connect() as conn:
                conn.execute('UPDATE alerts SET enabled=0 WHERE id=1')
            return Inventory('Monitor', True, '1', None, 'Found', 'new-in-stock')
        checker = Mock()
        checker.check.side_effect = pause
        sender = Mock()
        run_check(self.db, self.db.alert(1), checker, sender)
        sender.assert_not_called()

    def test_manual_check_cooldown(self):
        self.create()
        with self.db.connect() as conn:
            conn.execute("UPDATE alerts SET status='blocked',last_checked=?,next_check=? WHERE id=1", (time.time(), time.time()+900))
        self.post('/alerts/1/check')
        self.assertEqual(self.db.alert(1)['status'], 'blocked')


class MailTests(unittest.TestCase):
    @patch('stocksmart.mail.smtplib.SMTP')
    def test_starttls_and_login(self, smtp):
        settings = dict(DEFAULT_SETTINGS, enabled=True, sender='me@example.com', recipient='you@example.com',
                        username='me@example.com', password='app-password')
        send_message(settings, 'Test', 'Body')
        session = smtp.return_value.__enter__.return_value
        session.starttls.assert_called_once()
        session.login.assert_called_once_with('me@example.com', 'app-password')
        session.send_message.assert_called_once()

    @patch('stocksmart.mail.smtplib.SMTP_SSL')
    def test_ssl(self, smtp):
        settings = dict(DEFAULT_SETTINGS, enabled=True, sender='me@example.com', recipient='you@example.com', security='ssl', port=465)
        send_message(settings, 'Test', 'Body')
        session = smtp.return_value.__enter__.return_value
        session.starttls.assert_not_called()
        session.send_message.assert_called_once()



class FakeTime:
    def __init__(self):
        self.now = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeBrowser:
    def __init__(self, pages):
        self.pages = pages
        self.reads = 0
        self.urls = []
        self.current_url = URL + '?storeid=101'
        self.closed = False

    def set_page_load_timeout(self, seconds):
        pass

    def get(self, url):
        self.urls.append(url)
        self.current_url = url
        self.reads = 0

    @property
    def page_source(self):
        result = self.pages[min(self.reads, len(self.pages)-1)]
        self.reads += 1
        return result

    def quit(self):
        self.closed = True


class BrowserRecoveryTests(unittest.TestCase):
    def alert(self, open_box=False, product='685416'):
        return {'url': URL.replace('685416', product), 'product_id': product, 'store_id': '101', 'open_box': open_box}

    def test_transient_interstitial_can_finish_without_another_navigation(self):
        clock = FakeTime()
        browser = FakeBrowser(['<title>Just a moment...</title>', page()])
        checker = BrowserChecker(factory=lambda: browser, clock=clock.clock, sleep=clock.sleep, wait_seconds=3)
        self.assertTrue(checker.check(self.alert()).available)
        self.assertEqual(len(browser.urls), 1)
        self.assertNotEqual(clock.now, 0)
        checker.close()
        self.assertTrue(browser.closed)

    def test_persistent_challenge_has_bounded_wait(self):
        clock = FakeTime()
        browser = FakeBrowser(['<title>Just a moment...</title>'])
        checker = BrowserChecker(factory=lambda: browser, clock=clock.clock, sleep=clock.sleep, wait_seconds=3)
        with self.assertRaises(CheckFailure) as caught:
            checker.check(self.alert())
        self.assertEqual(caught.exception.status, 'blocked')
        self.assertEqual(clock.now, 3)
        self.assertEqual(browser.urls[-1], 'about:blank')
        self.assertEqual(sum('microcenter.com' in url for url in browser.urls), 1)

    def test_same_product_new_and_openbox_share_one_page(self):
        clock = FakeTime()
        browser = FakeBrowser([page(openbox='1 Open Box: from $99.00 ID: 1')])
        factory = Mock(return_value=browser)
        checker = BrowserChecker(factory=factory, clock=clock.clock, sleep=clock.sleep)
        self.assertTrue(checker.check(self.alert()).available)
        self.assertTrue(checker.check(self.alert(True)).available)
        self.assertEqual(len(browser.urls), 1)
        factory.assert_called_once()

    def test_distinct_products_reuse_browser_and_space_requests(self):
        clock = FakeTime()
        browser = FakeBrowser([page()])
        factory = Mock(return_value=browser)
        checker = BrowserChecker(factory=factory, clock=clock.clock, sleep=clock.sleep)
        checker.check(self.alert())
        checker.check(self.alert(product='685417'))
        self.assertEqual(len(browser.urls), 2)
        self.assertEqual(checker.last_navigation, 15)
        self.assertEqual(clock.now, 25)
        factory.assert_called_once()

    def test_new_stock_cache_waits_for_delayed_openbox_markup(self):
        clock = FakeTime()
        browser = FakeBrowser([page(), page(openbox='1 Open Box: from $99.00 ID: 1')])
        checker = BrowserChecker(factory=lambda: browser, clock=clock.clock, sleep=clock.sleep)
        self.assertTrue(checker.check(self.alert()).available)
        self.assertTrue(checker.check(self.alert(True)).available)
        self.assertEqual(len(browser.urls), 1)

    def test_normal_cloudflare_script_does_not_mark_a_product_blocked(self):
        html = page() + '<script>var x="cf-chl-foo just a moment";</script>'
        self.assertTrue(parse_inventory(html, '101').available)

    def test_lost_browser_restarts_on_next_check(self):
        from selenium.common.exceptions import WebDriverException
        clock = FakeTime()
        broken = Mock()
        broken.get.side_effect = WebDriverException('disconnected')
        replacement = FakeBrowser([page()])
        factory = Mock(side_effect=[broken, replacement])
        checker = BrowserChecker(factory=factory, clock=clock.clock, sleep=clock.sleep)
        with self.assertRaises(CheckFailure):
            checker.check(self.alert())
        broken.quit.assert_called_once()
        self.assertTrue(checker.check(self.alert()).available)
        self.assertEqual(factory.call_count, 2)


class CooldownTests(unittest.TestCase):
    setUp = AppTests.setUp
    tearDown = AppTests.tearDown
    create = AppTests.create
    post = AppTests.post

    def test_shared_block_stops_other_alerts_and_survives_database_reopen(self):
        self.create()
        self.create(True)
        now = [time.time()]
        checker = Mock()
        checker.check.side_effect = CheckFailure('Verification required', 'blocked')
        run_check(self.db, self.db.alert(1), checker, clock=lambda: now[0])
        reopened = Database(self.temp.name)
        run_check(reopened, reopened.alert(2), checker, clock=lambda: now[0])
        self.assertEqual(checker.check.call_count, 1)
        self.assertEqual(reopened.retailer()['block_streak'], 1)
        self.assertEqual(reopened.alert(2)['status'], 'blocked')
        self.assertIsNone(reopened.alert(2)['last_checked'])
        self.assertIn(b'Micro Center is requesting verification', self.client.get('/').data)
        self.post('/alerts/2/check')
        self.assertGreater(self.db.alert(2)['next_check'], now[0])

    def test_repeated_blocks_back_off_and_success_resets_streak(self):
        self.create()
        now = [time.time()]
        checker = Mock()
        checker.check.side_effect = CheckFailure('Verification required', 'blocked')
        for expected in [900, 1800, 3600, 7200, 14400, 14400]:
            run_check(self.db, self.db.alert(1), checker, clock=lambda: now[0])
            self.assertEqual(self.db.retailer()['blocked_until'] - now[0], expected)
            now[0] = self.db.retailer()['blocked_until'] + 1
        checker.check.side_effect = None
        checker.check.return_value = Inventory('Monitor', False, '0', None, 'Sold out', 'empty')
        run_check(self.db, self.db.alert(1), checker, clock=lambda: now[0])
        self.assertEqual(self.db.retailer()['block_streak'], 0)
        self.assertEqual(self.db.retailer()['blocked_until'], 0)


if __name__ == '__main__':
    unittest.main()

import re
import smtplib
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from stocksmart import create_app
from stocksmart.db import Database, DEFAULT_SETTINGS
from stocksmart.inventory import CheckFailure, Inventory, parse_inventory, product_url, validate_url
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
        sender = Mock()
        checker = Mock()
        checker.check.return_value = Inventory('Monitor', True, '1', '$200.00', '1 open box', 'listing-a')
        run_check(self.db, self.db.alert(1), checker, sender)
        self.assertEqual(sender.call_count, 1)
        run_check(self.db, self.db.alert(1), checker, sender)
        self.assertEqual(sender.call_count, 1)
        checker.check.side_effect = CheckFailure('Verification', 'blocked')
        run_check(self.db, self.db.alert(1), checker, sender)
        self.assertEqual(self.db.alert(1)['status'], 'blocked')
        self.assertEqual(self.db.alert(1)['last_sent_signature'], 'listing-a')
        self.assertGreater(self.db.alert(1)['next_check'] - time.time(), 895)
        checker.check.side_effect = None
        run_check(self.db, self.db.alert(1), checker, sender)
        self.assertEqual(sender.call_count, 1)
        checker.check.return_value = Inventory('Monitor', False, '0', None, '0 open box', 'empty')
        run_check(self.db, self.db.alert(1), checker, sender)
        self.assertIsNone(self.db.alert(1)['last_sent_signature'])
        checker.check.return_value = Inventory('Monitor', True, '1', '$200.00', '1 open box', 'listing-b')
        sender.side_effect = smtplib.SMTPAuthenticationError(535, b'secret provider details')
        run_check(self.db, self.db.alert(1), checker, sender)
        self.assertIsNone(self.db.alert(1)['last_sent_signature'])
        self.assertNotIn('secret', self.db.alert(1)['notification_error'])
        sender.side_effect = None
        run_check(self.db, self.db.alert(1), checker, sender)
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


if __name__ == '__main__':
    unittest.main()

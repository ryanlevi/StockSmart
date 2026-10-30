"""HTTP product HTML first; a browser is optional for incomplete successful HTML.

These are public product pages, not an undocumented inventory API. Explicit
denials and rate limits end the batch and never trigger browser escalation.
"""
import time
import math
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import requests

from .inventory import BrowserChecker, CheckFailure, parse_inventory, product_url, validate_url


def retry_after_seconds(value, now=None):
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, date.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return 0


class BatchChecker:
    def __init__(self, profile_dir=None, transport='http', *, session_factory=requests.Session,
                 browser_factory=BrowserChecker, clock=time.monotonic, sleep=time.sleep):
        self.session = session_factory()
        self.session.headers.update({'User-Agent': 'StockSmart/2 (+https://github.com/ryanlevi/StockSmart)',
                                     'Accept': 'text/html,application/xhtml+xml'})
        self.profile_dir, self.transport = profile_dir, transport
        self.browser_factory, self.browser = browser_factory, None
        self.clock, self.sleep = clock, sleep
        self.last_request = None
        self.pages = {}
        self.failures = {}
        self.browser_keys = set()
        self.http_requests = 0

    @property
    def browser_requests(self):
        return self.browser.browser_requests if self.browser else 0

    def close(self):
        try:
            self.session.close()
        finally:
            if self.browser:
                self.browser.close()

    def _pace(self):
        if self.last_request is not None:
            self.sleep(max(0, 15 - (self.clock() - self.last_request)))
        self.last_request = self.clock()

    def _fetch(self, alert):
        # Fetch one ordinary product document for both stock types; the primary
        # purchase area includes the separate open-box count when it exists.
        url = product_url(alert['url'], alert['store_id'])
        self.session.cookies.set('storeSelected', alert['store_id'], domain='.microcenter.com', path='/')
        for _ in range(4):
            self._pace()
            self.http_requests += 1
            try:
                response = self.session.get(url, timeout=(5, 20), allow_redirects=False, stream=True)
                try:
                    retry = retry_after_seconds(response.headers.get('Retry-After'))
                    if (response.status_code in {401, 403, 429}
                            or response.headers.get('cf-mitigated', '').lower() == 'challenge'):
                        message = ('Micro Center rate-limited the HTTP request.' if response.status_code == 429
                                   else 'Micro Center declined HTTP access or requested verification.')
                        raise CheckFailure(message, 'blocked', retry_after=retry)
                    if 500 <= response.status_code < 600 and retry:
                        raise CheckFailure('Micro Center requested a pause before retrying.', 'blocked', retry_after=retry)
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = urljoin(url, response.headers.get('Location', ''))
                        try:
                            _, product_id = validate_url(target)
                        except ValueError:
                            raise CheckFailure('Micro Center redirected away from the product page.')
                        if product_id != alert['product_id']:
                            raise CheckFailure('Micro Center redirected to a different product.')
                        url = target
                        continue
                    if response.status_code != 200:
                        raise CheckFailure(f'Micro Center returned HTTP {response.status_code}.', 'error')
                    if 'html' not in response.headers.get('Content-Type', '').lower():
                        raise CheckFailure('The product endpoint did not return HTML.')
                    body, size = [], 0
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > 3 * 1024 * 1024:
                            raise CheckFailure('Micro Center returned an unexpectedly large product document.')
                        body.append(chunk)
                    return b''.join(body).decode(response.encoding or 'utf-8', errors='replace')
                finally:
                    response.close()
            except requests.RequestException:
                raise CheckFailure('The HTTP stock request could not complete. Will retry with the next batch.', 'error')
        raise CheckFailure('Micro Center redirected too many times.')

    def check(self, alert):
        key = (alert['product_id'], alert['store_id'])
        if key in self.failures:
            raise self.failures[key]
        if key not in self.browser_keys:
            if key not in self.pages:
                try:
                    self.pages[key] = self._fetch(alert)
                except CheckFailure as exc:
                    self.failures[key] = exc
                    raise
            try:
                result = parse_inventory(self.pages[key], alert['store_id'], bool(alert['open_box']))
                result.transport = 'http'
                return result
            except CheckFailure as exc:
                if exc.status == 'blocked' or self.transport == 'http':
                    self.failures[key] = exc
                    raise
                # Only an ordinary HTTP 200 with incomplete inventory markup may
                # use the optional browser renderer. Never escalate a denial.
                self.browser_keys.add(key)
        if self.browser is None:
            self.browser = self.browser_factory(self.profile_dir)
            self.browser.cache_ttl = float('inf')
        if key not in self.browser.cache:
            self._pace()
        result = self.browser.check(alert)
        result.transport = 'browser'
        return result

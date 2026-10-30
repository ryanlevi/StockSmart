"""Conservative, store-specific parsing; blocked/unknown never means sold out."""
import hashlib
import re
from pathlib import Path
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .stores import STORES, store_city


class CheckFailure(Exception):
    def __init__(self, message, status='unknown'):
        super().__init__(message)
        self.status = status


def validate_url(raw):
    try:
        parsed = urlsplit(raw.strip())
        port = parsed.port
    except ValueError:
        raise ValueError('Enter a valid Micro Center product link.')
    if (parsed.scheme != 'https' or parsed.hostname not in {'microcenter.com', 'www.microcenter.com'}
            or parsed.username or parsed.password or port not in {None, 443}):
        raise ValueError('Use an https://www.microcenter.com/product/… link.')
    match = re.fullmatch(r'/product/(\d{4,10})(?:/[^\s?#]*)?/?', parsed.path)
    if not match:
        raise ValueError('Paste the product page link, not a search or category page.')
    # Discard inherited store IDs, open-box flags, tracking parameters and fragments.
    return urlunsplit(('https', 'www.microcenter.com', parsed.path, '', '')), match.group(1)


def product_url(url, store_id, open_box=False):
    clean, _ = validate_url(url)
    query = {'storeid': store_id}
    if open_box:
        query['ob'] = '1'
    return clean + '?' + urlencode(query)


@dataclass
class Inventory:
    title: str
    available: bool
    quantity: str
    price: str | None
    detail: str
    signature: str


def parse_inventory(html, store_id, open_box=False):
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True)
    # Cloudflare scripts can also occur on a normal product page. Inspect visible
    # interstitial wording, not the mere presence of a script/"cf-chl" substring.
    if (re.search(r'just a moment|access denied|attention required',
                  soup.title.get_text() if soup.title else '', re.I)
            or re.search(r'performing security verification|verify you are (?:a )?human|'
                         r'checking your browser', text[:2000], re.I)):
        raise CheckFailure('Micro Center is requesting browser verification. No stock result was recorded.', 'blocked')
    heading = soup.find('h1')
    if not heading:
        raise CheckFailure('The product page did not contain a recognizable product heading.')
    title = heading.get_text(' ', strip=True)
    # Only inspect the primary product purchase area. Menus, other-store selectors,
    # recommendations, specs and reviews are not evidence of this item's stock.
    for node in soup(['script', 'style', 'nav', 'header', 'footer']):
        node.decompose()
    text = soup.get_text(' ', strip=True)
    start = text.find(title)
    area = text[start + len(title):] if start >= 0 else ''
    area = re.split(r'Specifications Section|Overview Section|Bundle\s*&\s*Save|'
                    r'Customers Also (?:Viewed|Bought)|Frequently Bought Together|'
                    r'You May Also Like|Recommended Products|Related Products', area, maxsplit=1, flags=re.I)[0]
    # A stock statement must explicitly identify the selected store.
    stock = re.search(r'(\d+\+?)\s+(?:NEW\s+)?IN STOCK\s+at\s+(.{1,80}?)\s+Store\b', area, re.I)
    sold = re.search(r'(?:SOLD OUT|OUT OF STOCK)\s+at\s+(.{1,80}?)\s+Store\b', area, re.I)
    city = stock.group(2) if stock else sold.group(1) if sold else None
    expected = store_city(store_id)
    normalize = lambda value: re.sub(r'[^a-z0-9]', '', value.lower())
    if not city:
        raise CheckFailure('Could not confirm inventory for the selected store. Will retry.')
    aliases = {normalize(expected)}
    if store_id == '075':
        aliases.update({'paterson', 'northjerseypaterson'})
    if normalize(city) not in aliases:
        raise CheckFailure('Micro Center returned inventory for a different store. Will retry.')
    if open_box:
        match = re.search(r'(\d+)\s+open[ -]?box\b(?:\s*:?\s*(?:from\s*)?\$([\d,]+\.\d{2}))?', area, re.I)
        quantity = int(match.group(1)) if match else 0
        price = '$' + match.group(2) if match and match.group(2) else None
        ids = sorted(set(re.findall(r'\bID\s*:\s*(\d+)\b', area)))
        signature = hashlib.sha256(f'{quantity}|{price}|{ids}'.encode()).hexdigest()
        detail = f'{quantity} open-box item(s) at {expected}' + (f', from {price}.' if price else '.')
        return Inventory(title, quantity > 0, str(quantity), price, detail, signature)
    quantity = stock.group(1) if stock else '0'
    available = int(quantity.rstrip('+')) > 0
    detail = f'{quantity} new item(s) in stock at {expected}.' if available else f'New stock is sold out at {expected}.'
    return Inventory(title, available, quantity, None, detail, 'new-in-stock')


class BrowserChecker:
    """One ordinary browser session, bounded waits, and a short per-product cache.

    No fingerprint spoofing, proxy rotation, or challenge-solving automation.
    Persistent browser data stays outside the repository in the private data dir.
    """
    def __init__(self, profile_dir=None, *, factory=None, clock=time.monotonic,
                 sleep=time.sleep, wait_seconds=30):
        self.driver_path = None
        self.browser = None
        self.profile_dir = Path(profile_dir).resolve() if profile_dir else None
        self.factory = factory
        self.clock = clock
        self.sleep = sleep
        self.wait_seconds = wait_seconds
        self.cache = {}
        self.last_navigation = None

    def close(self):
        browser, self.browser = self.browser, None
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass

    def _browser(self):
        if self.browser is None:
            if self.factory:
                self.browser = self.factory()
            else:
                from selenium import webdriver
                from selenium.webdriver.chrome.service import Service
                from webdriver_manager.chrome import ChromeDriverManager
                if self.driver_path is None:
                    self.driver_path = ChromeDriverManager().install()
                options = webdriver.ChromeOptions()
                options.add_argument('--headless=new')
                options.add_argument('--disable-dev-shm-usage')
                if self.profile_dir:
                    self.profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    self.profile_dir.chmod(0o700)
                    options.add_argument('--user-data-dir=' + str(self.profile_dir))
                self.browser = webdriver.Chrome(service=Service(self.driver_path), options=options)
            self.browser.set_page_load_timeout(45)
        return self.browser

    def check(self, alert):
        from selenium.common.exceptions import TimeoutException, WebDriverException
        key = (alert['product_id'], alert['store_id'])
        cached = self.cache.get(key)
        if cached and self.clock() - cached[0] < 60:
            return parse_inventory(cached[1], alert['store_id'], bool(alert['open_box']))
        # A single serialized worker spaces distinct navigations, regardless of alert count.
        if self.last_navigation is not None:
            self.sleep(max(0, 15 - (self.clock() - self.last_navigation)))
        try:
            browser = self._browser()
            self.last_navigation = self.clock()
            # The query selects the store. Avoid a homepage request for every alert;
            # the parser still requires the returned purchase region to name that store.
            browser.get(product_url(alert['url'], alert['store_id'], bool(alert['open_box'])))
            deadline = self.clock() + self.wait_seconds
            openbox_ready_at = self.clock() + min(10, self.wait_seconds)
            last_error = CheckFailure('The product inventory did not finish loading. Will retry.')
            while True:
                try:
                    _, actual_id = validate_url(browser.current_url)
                    if actual_id != alert['product_id']:
                        raise CheckFailure('Micro Center returned a different product. Will retry.')
                    html = browser.page_source
                    result = parse_inventory(html, alert['store_id'], bool(alert['open_box']))
                    # A new-stock result may share its HTML with an open-box alert.
                    # Let delayed open-box markup settle before caching that page.
                    if (alert['open_box'] and result.available) or self.clock() >= openbox_ready_at:
                        self.cache[key] = (self.clock(), html)
                        if len(self.cache) > 100:
                            del self.cache[min(self.cache, key=lambda k: self.cache[k][0])]
                        return result
                except ValueError:
                    last_error = CheckFailure('Micro Center redirected away from the product page. Will retry.')
                except CheckFailure as exc:
                    # A brief verification/loading interstitial may finish itself.
                    # Wait for its actual outcome; never click a verification control.
                    last_error = exc
                if self.clock() >= deadline:
                    if last_error.status == 'blocked':
                        # Stop the interstitial's own timers/network activity during
                        # the shared cooldown, keeping this profile's normal cookies.
                        browser.get('about:blank')
                    raise last_error
                self.sleep(min(1, max(0, deadline - self.clock())))
        except TimeoutException:
            self.close()
            raise CheckFailure('Micro Center timed out. Will retry automatically.', 'error')
        except WebDriverException:
            self.close()
            raise CheckFailure('The inventory browser disconnected. It will restart on the next check.', 'error')

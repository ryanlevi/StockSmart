"""Conservative, store-specific parsing; blocked/unknown never means sold out."""
import hashlib
import re
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
    if re.search(r'just a moment|performing security verification|verify you are (?:a )?human|'
                 r'checking your browser|access denied|cf-chl-',
                 (soup.title.get_text() if soup.title else '') + ' ' + html[:6000], re.I):
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
    def __init__(self):
        self.driver_path = None

    def check(self, alert):
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.chrome.service import Service
        from webdriver_manager.chrome import ChromeDriverManager
        if self.driver_path is None:
            self.driver_path = ChromeDriverManager().install()
        options = webdriver.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--disable-dev-shm-usage')
        # Run as the unprivileged service user, with Chrome's sandbox enabled.
        try:
            with webdriver.Chrome(service=Service(self.driver_path), options=options) as browser:
                browser.set_page_load_timeout(45)
                browser.get('https://www.microcenter.com/?storeid=' + alert['store_id'])
                if 'just a moment' in browser.title.lower():
                    raise CheckFailure('Micro Center is requesting browser verification. Will retry automatically.', 'blocked')
                browser.add_cookie({'name': 'storeSelected', 'value': alert['store_id'],
                                    'domain': '.microcenter.com', 'path': '/', 'secure': True})
                browser.get(product_url(alert['url'], alert['store_id'], bool(alert['open_box'])))
                try:
                    _, actual_id = validate_url(browser.current_url)
                except ValueError:
                    raise CheckFailure('Micro Center redirected away from the product page. Will retry.')
                if actual_id != alert['product_id']:
                    raise CheckFailure('Micro Center returned a different product. Will retry.')
                # Wait for the purchase region rather than assuming page load means inventory is ready.
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    html = browser.page_source
                    if re.search(r'just a moment|performing security verification', html, re.I):
                        raise CheckFailure('Micro Center is requesting browser verification. Will retry automatically.', 'blocked')
                    try:
                        result = parse_inventory(html, alert['store_id'], bool(alert['open_box']))
                        # Do not conclude absent open-box stock before delayed markup can load.
                        if not alert['open_box'] or result.available or time.monotonic() > deadline - 2:
                            return result
                    except CheckFailure as exc:
                        if exc.status == 'blocked':
                            raise
                    time.sleep(1)
                return parse_inventory(browser.page_source, alert['store_id'], bool(alert['open_box']))
        except TimeoutException:
            raise CheckFailure('Micro Center timed out. Will retry automatically.', 'error')
        except WebDriverException:
            raise CheckFailure('The inventory browser could not complete the check. Will retry.', 'error')

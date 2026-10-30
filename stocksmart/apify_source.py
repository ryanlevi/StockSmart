"""Store-scoped Micro Center inventory through the Apify data provider."""
import hashlib
import re

import requests

from .inventory import CheckFailure, Inventory, product_url


ACTOR_ENDPOINT = 'https://api.apify.com/v2/acts/memo23~microcenter-product-scraper/run-sync-get-dataset-items'
RESULT_FIELDS = ','.join((
    'productId', 'name', 'url', 'storeId', 'storeName', 'stockStatus', 'stockCount',
    'stockCountIsMinimum', 'stockText', 'openBoxCount', 'openBoxFromPrice', 'openBoxOffers', 'scrapedAt',
))


class ApifyStoreChecker:
    """One hosted Actor run per store batch, with one result reused by stock type."""

    def __init__(self, token, max_charge=0.10, *, session_factory=requests.Session):
        self.token = token
        self.max_charge = float(max_charge)
        self.session = session_factory()
        self.session.headers.update({'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
        self.results = {}
        self.failure = None
        self.prepared = False
        self.http_requests = 0
        self.browser_requests = 0

    def close(self):
        self.session.close()

    def prepare(self, alerts):
        """Fetch each unique product once for this store; never retry a paid run."""
        if self.prepared:
            return
        self.prepared = True
        unique = {}
        for alert in alerts:
            unique.setdefault(alert['product_id'], alert)
        if not unique:
            return
        if not self.token:
            self.failure = CheckFailure('Add an Apify API token in Global settings to run automated checks.', 'error')
            return
        store_id = alerts[0]['store_id']
        payload = {
            'startUrls': [{'url': product_url(alert['url'], store_id)} for alert in unique.values()],
            'scrapeMode': 'detail',
            'storeId': store_id,
            'maxItems': len(unique),
            'inStockOnly': False,
            'maxConcurrency': 1,
            'cacheMaxAgeHours': 0,
        }
        params = {
            'timeout': 300,
            'memory': 1024,
            'maxItems': len(unique),
            'maxTotalChargeUsd': f'{self.max_charge:.2f}',
            'fields': RESULT_FIELDS,
        }
        self.http_requests += 1
        try:
            response = self.session.post(ACTOR_ENDPOINT, params=params, json=payload, timeout=(10, 310))
            if response.status_code == 401:
                self.failure = CheckFailure('Apify rejected the saved API token.', 'error')
                return
            if response.status_code == 402:
                self.failure = CheckFailure('Apify could not start the run within the configured charge cap or account balance.', 'error')
                return
            if response.status_code == 403:
                self.failure = CheckFailure('The Apify account cannot run this Actor. Check its access and billing settings.', 'error')
                return
            if response.status_code == 429:
                self.failure = CheckFailure('Apify rate-limited this run. It will retry on the next scheduled batch.', 'error')
                return
            if not response.ok:
                self.failure = CheckFailure(f'Apify returned HTTP {response.status_code}. The batch will retry on its next run.', 'error')
                return
            rows = response.json()
        except (requests.RequestException, ValueError):
            self.failure = CheckFailure('The Apify stock request did not return usable data. The batch will retry on its next run.', 'error')
            return
        if not isinstance(rows, list):
            self.failure = CheckFailure('Apify returned an unexpected stock-data format.', 'error')
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            product_id = str(row.get('productId') or '')
            if product_id in unique and str(row.get('storeId') or '') == store_id:
                self.results[product_id] = row

    def check(self, alert):
        if self.failure:
            raise self.failure
        row = self.results.get(str(alert['product_id']))
        if not row:
            raise CheckFailure('Apify returned no matching product for this store; no stock result was recorded.', 'unknown')
        if str(row.get('storeId') or '') != alert['store_id']:
            raise CheckFailure('Apify returned inventory for a different store; no stock result was recorded.', 'unknown')

        title = str(row.get('name') or alert['title'])[:220]
        store_name = str(row.get('storeName') or alert['store_id'])
        if alert['open_box']:
            offers = row.get('openBoxOffers') if isinstance(row.get('openBoxOffers'), list) else []
            count_value = row.get('openBoxCount')
            count = int(count_value) if count_value is not None else len(offers)
            prices = [offer.get('price') for offer in offers if isinstance(offer, dict) and offer.get('price') is not None]
            price_value = row.get('openBoxFromPrice')
            if price_value is None and prices:
                price_value = min(prices)
            match = re.search(r'\d+(?:\.\d{1,2})?', str(price_value).replace(',', '')) if price_value is not None else None
            price = f'${float(match.group()):,.2f}' if match else None
            offer_ids = sorted(str(offer['openBoxId']) for offer in offers
                               if isinstance(offer, dict) and offer.get('openBoxId'))
            signature = hashlib.sha256(f'{count}|{price}|{offer_ids}'.encode()).hexdigest()
            detail = (f'{count} open-box item(s) at {store_name}' + (f', from {price}.' if price else '.')
                      if count else f'No open-box inventory at {store_name}.')
            return Inventory(title, count > 0, str(count), price, detail, signature, 'apify')

        status = str(row.get('stockStatus') or '').upper()
        if status == 'IN_STOCK':
            value = row.get('stockCount')
            quantity = f'{int(value)}+' if row.get('stockCountIsMinimum') and value is not None else str(value or 'In stock')
            detail = f'{quantity} new item(s) in stock at {store_name}.'
            return Inventory(title, True, quantity, None, detail, 'new-in-stock', 'apify')
        if status == 'SOLD_OUT':
            return Inventory(title, False, '0', None, f'New stock is sold out at {store_name}.', 'new-in-stock', 'apify')
        raise CheckFailure('Apify could not confirm the selected store’s stock status.', 'unknown')

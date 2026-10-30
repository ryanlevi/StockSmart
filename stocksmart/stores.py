"""Store IDs from Micro Center's official store selector, verified 2026-10-30.

Source: https://www.microcenter.com/site/stores/default.aspx
Keep leading zeroes: these values are used in storeSelected and storeid.
"""
STORES = {
    '205': 'AZ — Phoenix', '215': 'TX — Austin',
    '195': 'CA — Santa Clara', '101': 'CA — Tustin',
    '181': 'CO — Denver', '185': 'FL — Miami',
    '065': 'GA — Duluth', '041': 'GA — Marietta',
    '151': 'IL — Chicago', '025': 'IL — Westmont',
    '165': 'IN — Indianapolis', '191': 'KS — Overland Park',
    '121': 'MA — Cambridge', '085': 'MD — Rockville',
    '125': 'MD — Parkville', '055': 'MI — Madison Heights',
    '045': 'MN — St. Louis Park', '095': 'MO — Brentwood',
    '175': 'NC — Charlotte', '075': 'NJ — North Jersey',
    '171': 'NY — Westbury', '115': 'NY — Brooklyn',
    '145': 'NY — Flushing', '105': 'NY — Yonkers',
    '141': 'OH — Columbus', '051': 'OH — Mayfield Heights',
    '071': 'OH — Sharonville', '061': 'PA — St. Davids',
    '155': 'TX — Houston', '131': 'TX — Dallas',
    '081': 'VA — Fairfax',
}


def store_city(store_id):
    return STORES[store_id].split(' — ', 1)[1]

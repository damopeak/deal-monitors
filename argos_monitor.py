"""Argos -> Amazon flip monitor, fed by ArbiSource, posted to Discord.

Argos blocks automated browsers from its category pages, so the catalogue comes from
ArbiSource instead, from two sources:

  1. A full ArbiSource store scan of every Argos department, re-run every few hours.
     ArbiSource scrapes Argos and matches each product to Amazon on its own servers.
  2. ArbiSource's price-drop feed, filtered to Argos, checked every few minutes
     between scans.

Every result goes through the same filter as the other ArbiSource monitors
(arbisource_filter.py, settings under `arbisource: filter:` in config.yaml, with
per-Argos overrides under `argos: filter:`). Anything that passes gets Keepa's live
price, rank and sales added (1 token) and is posted in the Megamind layout with the
Keepa chart attached. The chart costs no tokens.

Profit is ArbiSource's own VAT-registered figure, unadjusted. ROI is that profit over
the price paid inc VAT, the same way ArbiSource works it out on scan results.

Config (config.yaml):
    argos:
      webhook_url: "https://discord.com/api/webhooks/..."

Run:
    python argos_monitor.py --once      # one cycle and exit
    python argos_monitor.py --dry-run   # evaluate the latest data, post nothing
    python argos_monitor.py --test      # send a test message
    python argos_monitor.py --service   # background mode (Windows startup)
"""
import argparse
import json
import logging
import os
import random
import socket
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import requests
import yaml
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.yaml')
STATE_FILE = os.path.join(BASE_DIR, 'argos_state.json')
LOG_FILE = os.path.join(BASE_DIR, 'argos_monitor.log')

load_dotenv(os.path.join(BASE_DIR, '.env'))

if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
_handlers = [logging.FileHandler(LOG_FILE, encoding='utf-8')]
if sys.stderr:  # pythonw has no console
    _handlers.append(logging.StreamHandler())
for _h in _handlers:  # only this script's lines, plus library warnings
    _h.addFilter(lambda r: r.name == 'argos' or r.levelno >= logging.WARNING)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=_handlers)
log = logging.getLogger('argos')

# Imported after logging is configured: these modules log but set up no handlers.
from arbisource_client import ArbiSourceClient, ArbiSourceError  # noqa: E402
from config_loader import read_yaml  # noqa: E402
import arbisource_filter as af  # noqa: E402
from arbisource_filter import _num  # noqa: E402

STORE = 'Argos'
LOCK_PORT = 47833
CDN_BASE = 'https://d3r1ok86eo9ahu.cloudfront.net'
KEEPA_API = 'https://api.keepa.com'
KEEPA_UK = 2

DEFAULTS = {
    'poll_minutes': 5,             # how often the price-drop feed is checked
    'scan_interval_hours': 6,      # how often a full Argos scan is started
    # Full departments. The "c-" categories in ArbiSource look like clearance subsets
    # of these, and Clothing is left out on purpose.
    'scan_categories': [
        'Technology', 'Home & Furniture', 'Toys', 'Sports & Leisure', 'Appliances',
        'Health & Beauty', 'Jewellery & Watches', 'Baby & Nursery', 'Garden & DIY',
        'Gifts & Occassions',  # sic, ArbiSource's spelling
    ],
    'store_cache_hours': 2,
    'amazon_cache_hours': 6,
    'queue_limit': 50,
    'max_scan_results': 30000,     # safety cap when paging through a finished scan
    'drop_pages': 2,               # pages of 80 price drops per poll
    # ArbiSource's price-drop feed runs 8-32 hours behind Argos. A deal whose Argos
    # price was last checked longer ago than this is skipped: it has often ended.
    'max_price_age_hours': 6,
    # Final gates, on ArbiSource's own figures.
    'min_roi_cash': 20.0,          # ArbiSource profit / price paid inc VAT, %
    'min_profit_now': 3.0,         # ArbiSource VAT-registered profit, £ per unit
    'keepa_verify': True,          # add Keepa's live price / rank / sales to each alert
    'keepa_reserve': 2,            # tokens left untouched (alerts still post without Keepa)
    'max_posts_per_cycle': 25,     # the rest wait in the queue for the next cycle
    'realert_drop_pct': 5.0,       # re-post an ASIN only if Argos gets this much cheaper
    'realert_days': 7,             # ...or after this many days
    'filter': {},                  # overrides on top of arbisource: filter
}


# ---------------------------------------------------------------- config / state

def load_config() -> dict:
    root = read_yaml(CONFIG_FILE)  # config.yaml + secrets from .env
    arb = root.get('arbisource') or {}
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg.update(root.get('argos') or {})
    cfg['webhook_url'] = os.environ.get('ARGOS_WEBHOOK_URL') or cfg.get('webhook_url')
    # ArbiSource's own profit figures are used as they are, so the flat inbound
    # deduction the other ArbiSource monitors apply is switched off here by default.
    cfg['filter'] = {**(arb.get('filter') or {}), 'inbound_per_unit': 0.0, **(cfg.get('filter') or {})}
    cfg['costs'] = arb.get('costs') or {}
    cfg['keepa_api_key'] = root.get('keepa_api_key')
    return cfg


def load_state() -> dict:
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
    state.setdefault('scan_id', None)
    state.setdefault('scan_started', 0)
    state.setdefault('processed_scans', [])
    state.setdefault('seen_drops', [])
    state.setdefault('alerted', {})    # ASIN -> {"price": float, "at": ts}
    state.setdefault('queue', [])      # passed deals waiting for Keepa tokens or post slots
    return state


def save_state(state: dict) -> None:
    state['processed_scans'] = state['processed_scans'][-50:]
    state['seen_drops'] = state['seen_drops'][-5000:]
    cutoff = time.time() - 60 * 86400
    state['alerted'] = {k: v for k, v in state['alerted'].items() if v['at'] >= cutoff}
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- ArbiSource

def price_drops(client: ArbiSourceClient, pages: int) -> list[dict]:
    """The price-drop feed, restricted to Argos server-side."""
    u = client.user_data()
    out = []
    for page in range(pages):
        body = {
            'sortConfig': [{'field': 'Most Recent', 'order': 'desc'}],
            'dropsPerPage': 80, 'page': page, 'type': 'matched',
            'hiddenStores': [], 'selectedStores': [STORE],
            'filterSettings': client.wide_filters(),
            'fixedCost': client.cost_overrides.get('fixed_cost', u.get('fixedCost', 0)),
            'inboundShippingCost': client.cost_overrides.get('inbound_shipping_cost', 0),
            'storageMonths': client.cost_overrides.get('storage_months', 0),
            'VATStatus': u.get('VATStatus', 'Registered'),
            'useAveragePrice': False, 'averageProfitDays': 90, 'hiddenStoreCategories': [],
        }
        got = client._call('POST', '/pricedrops/getPriceDrops', json=body).get('drops', [])
        out += [d for d in got if d.get('storeName') == STORE]
        if len(got) < 80:
            break
    return out


def start_scan(client: ArbiSourceClient, cfg: dict) -> str | None:
    """Start a full Argos store scan. Returns the scan id, or None if the queue is full."""
    busy = [s for s in client.scans(only_completed=False) if not s.get('completedDateTime')]
    if len(busy) >= cfg['queue_limit']:
        log.warning('ArbiSource scan queue full (%d) — will try again next cycle', len(busy))
        return None
    body = {
        'emailOnCompletion': False,
        'storeCache': cfg['store_cache_hours'] / 24.0,    # ArbiSource takes days
        'amazonCache': cfg['amazon_cache_hours'] / 24.0,
        'isAutoScan': False,
        'region': 'UK',
        'filterSettings': client.wide_filters(),
        'filterOption': 'none',
        'checkRestrictions': True,
        'excludedKeywords': [],
        'selectedStoresAndCategories': {STORE: cfg['scan_categories']},
        'storeDiscounts': {},
        'storePriceOverrides': {},
        'customCategoryLinks': {},
    }
    r = client._call('POST', '/scan/storeScan', json=body)
    scan = r[0] if isinstance(r, list) else r
    return str(scan.get('_id')) if scan and scan.get('_id') else None


def scan_status(client: ArbiSourceClient, scan_id: str) -> dict | None:
    return next((s for s in client.scans(only_completed=False) if str(s['_id']) == scan_id), None)


def scan_results(client: ArbiSourceClient, scan_id: str, cap: int) -> list[dict]:
    wide = client.wide_filters()
    out, skip = [], 0
    while skip < cap:
        batch = client.get_scan_results(scan_id, skip=skip, per_page=80, filter_settings=wide)
        if not batch:
            break
        out += batch
        skip += len(batch)
        if len(batch) < 80:
            break
    return [normalise(r) for r in out if r.get('storeName') == STORE]


def normalise(r: dict) -> dict:
    """Scan results use slightly different names from price drops; the filter expects the latter."""
    d = dict(r)
    d.setdefault('stockStatus', r.get('storeStockStatus'))
    if not d.get('storeDiscountedPrice'):
        d['storeDiscountedPrice'] = r.get('storePrice')
    if not d.get('storeDiscountedPriceExclVAT'):
        d['storeDiscountedPriceExclVAT'] = r.get('storePriceExclVAT')
    d.setdefault('reduction', 0)
    return d


# ---------------------------------------------------------------- Keepa

def keepa_now(key: str, asin: str) -> dict | None:
    """Live Amazon numbers for one ASIN. Costs 1 token (no Buy Box / offers add-ons)."""
    r = requests.get(f'{KEEPA_API}/product', timeout=60,
                     params={'key': key, 'domain': KEEPA_UK, 'asin': asin, 'stats': 90, 'history': 0})
    data = r.json()
    if data.get('error') or not data.get('products'):
        log.warning('Keepa %s: %s', asin, data.get('error') or 'no product')
        return {'tokens_left': data.get('tokensLeft', 0), 'ok': False}
    p = data['products'][0]
    cur = (p.get('stats') or {}).get('current') or []

    def price(i):
        v = cur[i] if i < len(cur) else -1
        return v / 100.0 if v and v > 0 else None

    amazon, new, fba = price(0), price(1), price(10)
    # What the Buy Box is realistically sitting at: the cheaper of Amazon and FBA
    # offers, falling back to the cheapest new offer of any kind.
    sell = min([x for x in (amazon, fba) if x] or [new or 0]) or None
    rank = cur[3] if len(cur) > 3 and cur[3] and cur[3] > 0 else None
    offers = cur[11] if len(cur) > 11 and cur[11] and cur[11] > 0 else None
    return {'ok': True, 'tokens_left': data.get('tokensLeft', 0), 'sell': sell,
            'amazon': amazon, 'bsr': rank, 'offers': offers,
            'monthly_sold': p.get('monthlySold') or None}


def keepa_tokens(key: str) -> int:
    r = requests.get(f'{KEEPA_API}/token', params={'key': key}, timeout=30).json()
    return int(r.get('tokensLeft') or 0)


def keepa_chart(key: str, asin: str) -> bytes | None:
    """Price/rank chart as a PNG. The graph endpoint costs no tokens.
    Downloaded and attached so the API key never appears in Discord."""
    try:
        r = requests.get(f'{KEEPA_API}/graphimage', timeout=60, params={
            'key': key, 'domain': KEEPA_UK, 'asin': asin, 'amazon': 1, 'new': 1, 'bb': 1,
            'fba': 1, 'salesrank': 1, 'range': 90, 'width': 500, 'height': 250})
    except requests.RequestException:
        return None
    return r.content if r.ok and r.headers.get('content-type', '').startswith('image') else None


# ---------------------------------------------------------------- deal maths

def price_gap(v: dict, live: float) -> float | None:
    """% the live Keepa price sits below the Amazon price ArbiSource costed the deal at."""
    if not v['sell'] or not live:
        return None
    return (v['sell'] - live) / v['sell'] * 100.0


def evaluate(d: dict, cfg: dict) -> dict:
    v = af.evaluate(d, cfg['filter'], vat_registered=True)
    m = v['metrics']
    # ArbiSource's own VAT-registered profit, unadjusted. ROI is that profit over the
    # price paid inc VAT, which is how ArbiSource works ROI out on scan results.
    v['profit'] = _num(d.get('profitVAT'))
    v['roi_cash'] = v['profit'] / m['cash_price'] * 100.0 if m['cash_price'] else 0.0
    v['profit_90'] = _num(d.get('profitVAT90'))
    v['roi_cash_90'] = v['profit_90'] / m['cash_price'] * 100.0 if m['cash_price'] else 0.0
    v['sell'] = m['current_sell']
    v['keepa'] = None
    return v


def price_age_hours(d: dict) -> float | None:
    """How long ago ArbiSource last checked the Argos price."""
    t = d.get('storeInfoFrom') or d.get('lastCheckedStoreStock')
    if not t:
        return None
    try:
        seen = datetime.fromisoformat(str(t).replace('Z', '+00:00'))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - seen).total_seconds() / 3600.0


def final_gate(v: dict, cfg: dict) -> str | None:
    if v['profit'] < cfg['min_profit_now']:
        return f"profit £{v['profit']:.2f} < £{cfg['min_profit_now']}"
    if v['roi_cash'] < cfg['min_roi_cash']:
        return f"ROI {v['roi_cash']:.0f}% < {cfg['min_roi_cash']}%"
    # Amazon may sit above its 90-day average (no spike gate for Argos), so the deal
    # has to hold up at that average too.
    if v['profit_90'] < cfg['min_profit_now']:
        return f"profit at 90d avg £{v['profit_90']:.2f} < £{cfg['min_profit_now']}"
    if v['roi_cash_90'] < cfg['min_roi_cash']:
        return f"ROI at 90d avg {v['roi_cash_90']:.0f}% < {cfg['min_roi_cash']}%"
    return None


def already_alerted(state: dict, d: dict, cfg: dict) -> bool:
    prev = state['alerted'].get(d.get('ASIN'))
    if not prev:
        return False
    price = _num(d.get('storeDiscountedPrice')) or _num(d.get('storePrice'))
    cheaper = price <= prev['price'] * (1 - cfg['realert_drop_pct'] / 100.0)
    old = time.time() - prev['at'] > cfg['realert_days'] * 86400
    return not (cheaper or old)


# ---------------------------------------------------------------- Discord

def _short(n) -> str:
    if n is None:
        return 'n/a'
    return f'{n / 1000:.0f}k' if n >= 10000 else (f'{n / 1000:.1f}k' if n >= 1000 else str(n))


def build_embed(d: dict, v: dict) -> dict:
    m = v['metrics']
    k = v.get('keepa') or {}
    asin = d.get('ASIN', '')
    ean = d.get('storeEAN') or ''
    title = (d.get('storeProductName') or d.get('amazonProductName') or 'Unknown')[:250]

    if m['ean_match']:
        match = f'EAN match ✅ ({ean})' if ean else 'EAN match ✅'
    else:
        match = f"{m['confidence'] * 100:.0f}% (no EAN match)"

    bsr = k.get('bsr') or m['bsr']
    sales = k.get('monthly_sold') or m['monthly_sold'] or int(_num(d.get('salesEstimate')))
    sellers = k.get('offers') or m['sellers']
    info = [f"Sales Rank: {_short(bsr)}" + (f" ({m['bsr_pct']:.2f}% of category)" if m['bsr_pct'] else ''),
            f"Est. Sales: {sales}+/mo" if sales else 'Est. Sales: n/a',
            f"Unique Sellers: {sellers}" + (f" ({m['fba_offers']} FBA)" if m['fba_offers'] else '')]

    buy = f"Buy (incl. VAT): £{m['cash_price']:.2f}"
    if m['discounted']:
        buy += f" (was £{m['list_price']:.2f})"
    pricing = [buy, f"Buy Box: £{v['sell']:.2f}" if v['sell'] else 'Buy Box: n/a']
    if m['avg90']:
        pricing.append(f"90-day avg: £{m['avg90']:.2f}")
    age = price_age_hours(d)
    if age is not None:
        pricing.insert(1, f"Argos price checked {age:.0f}h ago" if age >= 1 else 'Argos price checked under 1h ago')
    if k.get('ok') and k.get('sell'):
        pricing.append(f"Keepa live: £{k['sell']:.2f}")

    profit = [f"VAT: £{v['profit']:.2f} | ROI: {v['roi_cash']:.2f}%",
              f"At 90-day avg: £{v['profit_90']:.2f} | ROI: {v['roi_cash_90']:.0f}%"]

    q = urllib.parse.quote
    links = [f"[Argos]({d['storeUrl']})" if d.get('storeUrl') else None,
             f"[Amazon](https://www.amazon.co.uk/dp/{asin})",
             f"[Keepa](https://keepa.com/#!product/2-{asin})",
             f"[SellerAMP](https://sas.selleramp.com/sas/lookup?SasLookup%5Bsearch_term%5D={asin})",
             f"[Google](https://www.google.com/search?q={q(ean or title)})"]

    warnings = []
    if not m['in_stock']:
        warnings.append(f"⚠️ {d.get('stockStatus') or 'Out of stock'} at Argos")
    if m['ip_status'] not in ('green', 'unknown'):
        warnings.append(f"⚠️ IP status {m['ip_status']}: check before buying")
    if d.get('restricted'):
        warnings.append('⚠️ Restricted on your account')
    if d.get('hazmat'):
        warnings.append('⚠️ Dangerous goods (needs DG approval)')
    if v.get('suspect_price'):
        warnings.append(f"⚠️ {m['reduction']:.0f}% off: may be a pricing error, check Argos")
    gap = price_gap(v, k.get('sell')) if k.get('ok') else None
    if gap is not None and gap >= 10:
        warnings.append(f"⚠️ Amazon now £{k['sell']:.2f}, {gap:.0f}% below the price ArbiSource used")

    fields = [
        {'name': 'Store', 'value': STORE, 'inline': True},
        {'name': 'ASIN', 'value': f'`{asin}`', 'inline': True},
        {'name': 'Amazon Title', 'value': str(m['amazon_name'] or 'n/a')[:300], 'inline': False},
        {'name': 'Match Strength', 'value': match, 'inline': False},
        {'name': 'Product Information', 'value': '\n'.join(info), 'inline': False},
        {'name': 'Pricing Information', 'value': '\n'.join(pricing), 'inline': False},
        {'name': 'Profit & ROI', 'value': '\n'.join(profit), 'inline': False},
        {'name': 'Links', 'value': ' | '.join(x for x in links if x), 'inline': False},
    ]
    if warnings:
        fields.append({'name': 'Check', 'value': '\n'.join(warnings), 'inline': False})
    fields.append({'name': 'Important Note', 'inline': False, 'value':
                   'Product matches may sometimes be inaccurate due to variation differences. '
                   'Please do your own research before buying to ensure the product is correct.'})

    embed = {
        'title': title,
        'color': 0xFF8800 if warnings else (0xFFD700 if v['tier'] == 'elite' else 0xD42114),  # Argos red
        'fields': fields,
        'footer': {'text': f"Argos Monitor • {v['source']} • score {v['score']:.0f}"},
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    if d.get('storeUrl'):
        embed['url'] = d['storeUrl']
    img = d.get('amazonImageUrl') or d.get('storeImageUrl')
    if img:
        embed['thumbnail'] = {'url': img if img.startswith('http') else f"{CDN_BASE}/{img.lstrip('/')}"}
    return embed


def post(webhook: str, payload: dict, chart: bytes | None = None) -> bool:
    for _ in range(3):
        try:
            if chart:
                payload['embeds'][0]['image'] = {'url': 'attachment://chart.png'}
                r = requests.post(webhook, timeout=30, data={'payload_json': json.dumps(payload)},
                                  files={'files[0]': ('chart.png', chart, 'image/png')})
            else:
                r = requests.post(webhook, json=payload, timeout=30)
        except requests.RequestException as e:
            log.warning('Discord post failed: %s', e)
            return False
        if r.status_code in (200, 204):
            return True
        if r.status_code == 429:
            time.sleep(float(r.json().get('retry_after', 2)) + 0.5)
            continue
        log.warning('Discord returned HTTP %s: %s', r.status_code, r.text[:200])
        return False
    return False


# ---------------------------------------------------------------- one cycle

def collect(client: ArbiSourceClient, cfg: dict, state: dict, dry_run: bool = False) -> list[tuple[dict, str]]:
    """New Argos products to judge: a finished scan's results plus fresh price drops."""
    found = []

    # 1) Full scans.
    sid = state['scan_id']
    if sid:
        s = scan_status(client, sid)
        if s is None:
            log.warning('scan %s has disappeared from ArbiSource', sid)
            state['scan_id'] = None
        elif s.get('completedDateTime') and sid not in state['processed_scans']:
            results = scan_results(client, sid, cfg['max_scan_results'])
            log.info('scan %s finished: %s Argos products, %d matched results',
                     sid, s.get('numberOfStoreProducts'), len(results))
            found += [(r, 'full scan') for r in results]
            state['processed_scans'].append(sid)
            state['scan_id'] = None
    due = time.time() - state['scan_started'] > cfg['scan_interval_hours'] * 3600
    if not state['scan_id'] and due and not dry_run:
        new_id = start_scan(client, cfg)
        if new_id:
            state['scan_id'], state['scan_started'] = new_id, time.time()
            log.info('started full Argos scan %s (%d departments)', new_id, len(cfg['scan_categories']))

    # 2) Price drops since last time.
    seen = set(state['seen_drops'])
    for d in price_drops(client, cfg['drop_pages']):
        key = str(d.get('_id') or f"{d.get('ASIN')}|{d.get('storeDiscountedPrice')}")
        if key not in seen:
            seen.add(key)
            state['seen_drops'].append(key)
            found.append((d, 'price drop'))
    return found


def run_cycle(client: ArbiSourceClient, cfg: dict, state: dict, dry_run: bool = False) -> None:
    candidates = []
    stale = 0
    for d, source in collect(client, cfg, state, dry_run):
        if already_alerted(state, d, cfg):
            continue
        age = price_age_hours(d)
        if age is not None and age > cfg['max_price_age_hours']:
            stale += 1
            continue
        v = evaluate(d, cfg)
        if v['pass'] and not final_gate(v, cfg):
            v['source'] = source
            candidates.append({'d': d, 'v': {k: x for k, x in v.items() if k != 'metrics'}})
    if stale:
        log.info('%d product(s) skipped: Argos price last checked over %sh ago', stale, cfg['max_price_age_hours'])
    if candidates:
        log.info('%d new candidate(s) passed the filter', len(candidates))

    # Queue first (older), then new ones, best first; one entry per ASIN.
    pool, asins = [], set()
    for c in sorted(state['queue'] + candidates, key=lambda c: -c['v']['score']):
        if c['d'].get('ASIN') not in asins:
            asins.add(c['d'].get('ASIN'))
            pool.append(c)
    state['queue'] = []

    tokens = keepa_tokens(cfg['keepa_api_key']) if cfg['keepa_verify'] and cfg['keepa_api_key'] and pool else 0
    posted = 0
    for i, c in enumerate(pool):
        d = c['d']
        if posted >= cfg['max_posts_per_cycle']:
            state['queue'] += pool[i:]
            log.info('%d deal(s) held for the next cycle (post cap)', len(pool) - i)
            break
        age = price_age_hours(d)
        if age is not None and age > cfg['max_price_age_hours']:
            log.info('drop %s from the queue: Argos price now %.0fh old', d.get('ASIN'), age)
            continue
        v = evaluate(d, cfg)  # rebuild metrics (not stored in the queue)
        v['source'] = c['v'].get('source', '?')

        # Keepa only adds live context, so a deal never waits for tokens.
        if cfg['keepa_verify'] and cfg['keepa_api_key'] and not dry_run and tokens > cfg['keepa_reserve']:
            k = keepa_now(cfg['keepa_api_key'], d['ASIN'])
            tokens = k.get('tokens_left', 0) if k else 0
            v['keepa'] = k if k and k.get('ok') else {'ok': False}

        m = v['metrics']
        log.info('DEAL [%s %.0f] %s | £%.2f -> £%.2f | £%.2f %.0f%% | %s',
                 v['tier'], v['score'], (d.get('storeProductName') or '')[:55], m['cash_price'],
                 v['sell'] or 0, v['profit'], v['roi_cash'], v['source'])
        if dry_run:
            continue
        chart = keepa_chart(cfg['keepa_api_key'], d['ASIN']) if cfg['keepa_api_key'] else None
        if post(cfg['webhook_url'], {'username': 'Argos Monitor', 'embeds': [build_embed(d, v)]}, chart):
            posted += 1
            state['alerted'][d['ASIN']] = {'price': m['cash_price'], 'at': time.time()}
        else:
            state['queue'].append(c)  # retry next cycle
        time.sleep(1.2)


# ---------------------------------------------------------------- loop / service

def run(cfg: dict, once: bool, dry_run: bool = False) -> None:
    client = ArbiSourceClient(cost_overrides=cfg.get('costs'))
    state = load_state()
    while True:
        try:
            run_cycle(client, cfg, state, dry_run)
            if not dry_run:
                save_state(state)
        except (ArbiSourceError, requests.RequestException) as e:
            log.warning('cycle failed: %s', e)
        if once or dry_run:
            return
        time.sleep(max(60, cfg['poll_minutes'] * 60 + random.randint(-20, 20)))
        cfg = load_config()  # pick up setting edits without a restart


def single_instance_lock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', LOCK_PORT))
    except OSError:
        return None
    return sock


def run_service() -> None:
    lock = single_instance_lock()
    if lock is None:
        log.info('Another Argos monitor is already running — exiting')
        return
    log.info('Service started')
    while True:
        cfg = load_config()
        if not cfg['webhook_url']:
            log.warning('Waiting for setup: argos: webhook_url in config.yaml')
            time.sleep(300)
            continue
        try:
            run(cfg, once=False)
        except Exception:
            log.exception('Monitor crashed — restarting in 60s')
            time.sleep(60)


def main() -> None:
    ap = argparse.ArgumentParser(description='Argos -> Amazon flips (via ArbiSource) to Discord.')
    ap.add_argument('--once', action='store_true', help='run a single cycle and exit')
    ap.add_argument('--dry-run', action='store_true', help='evaluate, post nothing, save nothing')
    ap.add_argument('--test', action='store_true', help='send a test message and exit')
    ap.add_argument('--service', action='store_true', help='background mode for Windows startup')
    args = ap.parse_args()

    if args.service:
        run_service()
        return
    cfg = load_config()
    if not cfg['webhook_url'] and not args.dry_run:
        sys.exit('No webhook configured. Add argos: webhook_url: "..." to config.yaml')
    if args.test:
        ok = post(cfg['webhook_url'], {'username': 'Argos Monitor',
                                       'content': f'✅ Argos monitor test ({datetime.now():%d %b %H:%M})'})
        sys.exit(0 if ok else 'Test post failed — check the webhook URL.')
    lock = single_instance_lock()  # held for the life of the process
    if lock is None:
        sys.exit('The Argos monitor is already running in the background. See argos_monitor.log.')
    run(cfg, once=args.once, dry_run=args.dry_run)


if __name__ == '__main__':
    main()

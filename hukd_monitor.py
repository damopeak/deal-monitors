"""
HotUKDeals -> Discord deal feed.

Every ~5 minutes, reads https://www.hotukdeals.com/hot (deals in the order they
turned hot, ~100 degrees) and posts each one to Discord, once, as it turns
hot: title, price, shop, voucher code, heat and a link. Only food and drink,
clothing, used/refurbished, digital items, travel and services are left out
and the filters are deliberately narrow: when in doubt, the deal is sent.
In-store deals are included and labelled.

Config (config.yaml):
    hukd:
      webhook_url: "https://discord.com/api/webhooks/..."

Run:
    python hukd_monitor.py            # watch
    python hukd_monitor.py --once     # single check
    python hukd_monitor.py --test     # send a test message
    python hukd_monitor.py --dry-run  # list the last hour's deals, post nothing
    python hukd_monitor.py --service  # background mode (Windows startup)
"""
import argparse
import html
import json
import logging
import os
import random
import re
import socket
import sys
import time
from datetime import datetime, timezone

import requests
import yaml

from config_loader import read_yaml

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.yaml')
STATE_FILE = os.path.join(BASE_DIR, 'hukd_hot_state.json')
LOG_FILE = os.path.join(BASE_DIR, 'hukd_monitor.log')

HUKD = 'https://www.hotukdeals.com'
USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36')
THREAD_RE = re.compile(r"data-vue3='([^']*ThreadMainListItemNormalizer[^']*)'")

LOCK_PORT = 47832
JITTER = 30

DEFAULTS = {
    'interval': 300,             # seconds between checks
    'max_age_hours': 3,          # ignore deals that turned hot longer ago (after downtime)
    'max_pages': 5,              # how far back to catch up after downtime (30 deals a page)
    'exclude_groups': ['travel', 'groceries'],  # groceries = food & drink, not resold
    'exclude_hosts': [
        'steampowered', 'epicgames', 'gog.com', 'humblebundle', 'fanatical', 'cdkeys',
        'eneba', 'g2a', 'instant-gaming', 'store.playstation', 'mytrip', 'traveljunction',
        'trainline', 'booking.com', 'expedia', 'lastminute', 'jet2', 'easyjet', 'ryanair',
        'tui.co.uk', 'audible', 'spotify', 'netflix', 'nowtv', 'apps.apple', 'play.google',
        'vinted', 'musicmagpie', 'backmarket',
        'amazon', 'amzn',  # the user doesn't want deals sold by Amazon
    ],
    # Title patterns (regex, case-insensitive) that mean "not a new physical
    # product". Kept narrow on purpose: a false skip loses a deal, a false
    # send only costs a glance.
    'exclude_patterns': [
        # digital
        r'\(steam\b', r'\bsteam (pc|key|code|game)\b', r'\bpc steam\b',
        r'\bdigital (code|download|copy|version|game|key|voucher)\b', r'\(digital\)',
        r'\bdlc\b', r'\bseason pass\b', r'\bgame pass\b', r'\bps plus\b',
        r'\bplaystation plus\b', r'\bxbox live\b', r'\bnintendo switch online\b',
        r'\bgift ?card\b', r'\be-?gift\b', r'\bkindle edition\b', r'\be-?book\b',
        r'\baudiobook\b', r'\bvpn\b', r'\b(cd|game|activation|licen[cs]e) key\b',
        r'\bsubscription\b', r'\bmembership\b',
        # used
        r'\brefurb(ished)?\b', r'\brenewed\b', r'\bpre-?owned\b', r'\bsecond[ -]hand\b',
        r'\bopen box\b', r'\bex-?display\b', r'\bb-grade\b', r'\bwarehouse deals?\b',
        r'\bamazon resale\b', r'\blike new\b', r'^used\b', r'\(used\b',
        r'\bused[ -]+(good|very good|acceptable|condition)\b',
        # services / not a product
        r'\bbroadband\b', r'\bsim only\b', r'\b\d+ ?(gb|month)s? (data )?contract\b',
        r'\binsurance\b', r'\bcredit card\b', r'\bmortgage\b', r'\btickets? (for|from)\b',
        r'\bday out\b', r'\bflights?\b.*\breturn\b', r'\bcar hire\b',
        # alcohol posted outside Groceries ("4.1% Vol", "5% ABV")
        r'\d(\.\d+)?\s?%\s?(abv|vol)\b', r'\babv\b',
    ],
    # Clothing and footwear. The full list applies only to HUKD's Fashion
    # category (so watches, bags, jewellery and sunglasses there still get
    # through); elsewhere only unambiguous clothing words count.
    'clothing_words_fashion': [
        'shirt', 't-shirt', 'tee', 'polo', 'top', 'blouse', 'jeans', 'trousers', 'chinos',
        'dress', 'skirt', 'hoodie', 'jumper', 'sweatshirt', 'sweater', 'cardigan', 'jacket',
        'coat', 'gilet', 'fleece', 'parka', 'socks', 'underwear', 'boxers', 'briefs', 'bra',
        'knickers', 'leggings', 'joggers', 'shorts', 'swimsuit', 'bikini', 'trainers',
        'sneakers', 'shoes', 'boots', 'sandals', 'slippers', 'sliders', 'loafers', 'hat',
        'cap', 'beanie', 'gloves', 'scarf', 'pyjamas', 'onesie', 'tracksuit', 'suit', 'vest',
        'blazer', 'waistcoat', 'nightie', 'bodysuit', 'romper', 'babygrow',
    ],
    'clothing_words_anywhere': [
        't-shirt', 'hoodie', 'jumper', 'sweatshirt', 'cardigan', 'leggings', 'joggers',
        'trainers', 'sneakers', 'pyjamas', 'onesie', 'tracksuit', 'jeans', 'trousers',
        'boxers', 'underwear', 'bikini', 'swimsuit',
    ],
}

if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
_handlers = [logging.FileHandler(LOG_FILE, encoding='utf-8')]
if sys.stderr:  # pythonw has no console
    _handlers.append(logging.StreamHandler())
for _h in _handlers:  # only this script's lines, plus library warnings
    _h.addFilter(lambda r: r.name == 'hukd' or r.levelno >= logging.WARNING)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=_handlers)
log = logging.getLogger('hukd')

session = requests.Session()
session.headers['User-Agent'] = USER_AGENT


# ---------------------------------------------------------------- config / state

def load_config() -> dict:
    root = read_yaml(CONFIG_FILE)  # config.yaml + secrets from .env
    cfg = dict(DEFAULTS)
    cfg.update(root.get('hukd') or {})
    cfg['webhook_url'] = os.environ.get('HUKD_WEBHOOK_URL') or cfg.get('webhook_url')
    return cfg


def load_state() -> dict | None:
    """None means 'never run before' — the first check seeds silently."""
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding='utf-8') as f:
        return json.load(f)


def save_state(state: dict) -> None:
    cutoff = time.time() - 3 * 86400
    state['seen'] = {k: v for k, v in state['seen'].items() if v >= cutoff}
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- HUKD

def fetch_page(page: int) -> list[dict]:
    r = session.get(f'{HUKD}/hot', params={'page': page}, timeout=30)
    r.raise_for_status()
    deals = []
    for m in THREAD_RE.finditer(r.text):
        try:
            t = json.loads(html.unescape(m.group(1)))['props']['thread']
        except (ValueError, KeyError):
            continue
        img = t.get('mainImage') or {}
        group = t.get('mainGroup') or {}
        deals.append({
            'id': str(t['threadId']),
            'title': t.get('title') or '',
            'url': f"{HUKD}/deals/{t.get('titleSlug')}-{t['threadId']}",
            'price': t.get('price') or 0,
            'merchant': (t.get('merchant') or {}).get('merchantName') or t.get('linkHost') or '?',
            'host': (t.get('linkHost') or '').lower(),
            'group': group.get('threadGroupUrlName') or '',
            'group_name': group.get('threadGroupName') or '',
            'heat': round(t.get('temperature') or 0),
            'voucher': t.get('voucherCode') or None,
            'published': t.get('hotDate') or t.get('publishedAt') or 0,  # when it turned hot
            'type': t.get('type') or '',
            'expired': bool(t.get('isExpired')),
            'local': bool(t.get('isLocal')),
            'image': (f"https://images.hotukdeals.com/{img['path']}/{img['name']}/re/300x300/qt/60/{img['name']}.jpg"
                      if img.get('path') else None),
        })
    return deals


def skip_reason(d: dict, cfg: dict) -> str | None:
    if d['type'] not in ('Deal', 'Voucher'):
        return f"type {d['type'] or '?'}"
    if d['expired']:
        return 'already expired'
    if d['group'] in cfg['exclude_groups']:
        return f"category {d['group']}"
    if any(h in d['host'] for h in cfg['exclude_hosts']):
        return f"retailer {d['host']}"
    title = d['title'].lower()
    pat = next((p for p in cfg['exclude_patterns'] if re.search(p, title)), None)
    if pat:
        return f'title matches {pat}'
    words = cfg['clothing_words_fashion'] if d['group'] == 'fashion' else cfg['clothing_words_anywhere']
    word = next((w for w in words if re.search(r'\b' + re.escape(w) + r'\b', title)), None)
    if word:
        return f'clothing ("{word}")'
    return None


# ---------------------------------------------------------------- Discord

def post(webhook: str, payload: dict) -> bool:
    for _ in range(3):
        try:
            r = session.post(webhook, json=payload, timeout=20)
        except requests.RequestException as e:
            log.warning('Discord post failed: %s', e)
            return False
        if r.status_code in (200, 204):
            return True
        if r.status_code == 429:  # rate limited: wait as told, then retry
            time.sleep(float(r.json().get('retry_after', 2)) + 0.5)
            continue
        log.warning('Discord returned HTTP %s: %s', r.status_code, r.text[:200])
        return False
    return False


def deal_embed(d: dict) -> dict:
    price = f"£{d['price']:.2f}" if d['price'] else 'See deal'
    fields = [
        {'name': 'Price', 'value': price, 'inline': True},
        {'name': 'Shop', 'value': d['merchant'], 'inline': True},
        {'name': 'Heat', 'value': f"{d['heat']}°", 'inline': True},
    ]
    if d.get('voucher'):
        fields.append({'name': 'Code', 'value': f"`{d['voucher']}`", 'inline': True})
    if d['local']:
        fields.append({'name': '📍 In store', 'value': 'Local / in-store deal: see the post for which store',
                       'inline': False})
    embed = {
        'title': d['title'][:250],
        'url': d['url'],
        'color': 0xF97316,  # HUKD orange
        'fields': fields,
        'footer': {'text': 'HUKD'},
        'timestamp': datetime.fromtimestamp(d['published'], timezone.utc).isoformat(),
    }
    if d['group_name']:
        embed['description'] = d['group_name']
    if d.get('image'):
        embed['thumbnail'] = {'url': d['image']}
    return embed


def post_deals(cfg: dict, deals: list[dict]) -> bool:
    """Discord allows 10 embeds and ~6000 characters per message."""
    ok, batch, size = True, [], 0
    for d in deals:
        e = deal_embed(d)
        n = len(json.dumps(e, ensure_ascii=False))
        if batch and (len(batch) == 10 or size + n > 5500):
            ok = post(cfg['webhook_url'], {'username': 'HUKD Monitor', 'embeds': batch}) and ok
            batch, size = [], 0
        batch.append(e)
        size += n
    if batch:
        ok = post(cfg['webhook_url'], {'username': 'HUKD Monitor', 'embeds': batch}) and ok
    return ok


# ---------------------------------------------------------------- one check

def check(cfg: dict, state: dict | None, dry_run: bool = False) -> dict:
    first_run = state is None
    state = state or {'seen': {}}
    seen = state['seen']
    now = time.time()

    deals = fetch_page(1)
    page = 1
    while page < (3 if dry_run else cfg['max_pages']):
        # Keep reading back while a whole page is new (busy spell, or the PC
        # was off) so nothing is missed. The dry run reads a fixed 3 pages.
        if not dry_run and (first_run or not all(d['id'] not in seen for d in deals[-30:])):
            break
        page += 1
        more = fetch_page(page)
        if not more:
            break
        deals += more

    if dry_run:
        fresh = [d for d in deals if now - d['published'] < 3600]
    else:
        fresh = [d for d in deals if d['id'] not in seen]
        for d in fresh:
            seen[d['id']] = now
        if first_run:
            log.info('First run: remembered %d current hot deals, will post newly hot ones from now', len(fresh))
            return state

    keep = []
    for d in sorted(fresh, key=lambda x: x['published']):
        reason = 'older than max_age_hours' if now - d['published'] > cfg['max_age_hours'] * 3600 \
            else skip_reason(d, cfg)
        if reason:
            log.info('skip  %-7s %s — %s', d['id'], d['title'][:60], reason)
        else:
            keep.append(d)

    for d in keep:
        log.info('post  %-7s %s — £%s @ %s', d['id'], d['title'][:60], d['price'], d['merchant'])

    if keep and not dry_run and not post_deals(cfg, keep):
        for d in keep:  # not posted: forget them so the next check retries
            seen.pop(d['id'], None)
    return state


# ---------------------------------------------------------------- loop / service

def run(cfg: dict, once: bool) -> None:
    state = load_state()
    while True:
        try:
            state = check(cfg, state)
            save_state(state)
        except requests.RequestException as e:
            log.warning('HUKD fetch failed: %s', e)
        if once:
            return
        time.sleep(max(60, cfg['interval'] + random.randint(-JITTER, JITTER)))
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
        log.info('Another HUKD monitor is already running — exiting')
        return
    log.info('Service started')
    while True:
        cfg = load_config()
        if not cfg['webhook_url']:
            log.warning('Waiting for setup: hukd: webhook_url in config.yaml')
            time.sleep(300)
            continue
        try:
            run(cfg, once=False)
        except Exception:
            log.exception('Monitor crashed — restarting in 60s')
            time.sleep(60)


def main() -> None:
    ap = argparse.ArgumentParser(description='Post new HotUKDeals deals to Discord.')
    ap.add_argument('--once', action='store_true', help='run a single check and exit')
    ap.add_argument('--test', action='store_true', help='send a test message and exit')
    ap.add_argument('--dry-run', action='store_true', help='check the last hour of deals, post nothing')
    ap.add_argument('--service', action='store_true', help='background mode for Windows startup')
    args = ap.parse_args()

    if args.service:
        run_service()
        return
    cfg = load_config()
    if args.dry_run:
        check(cfg, None, dry_run=True)
        return
    if not cfg['webhook_url']:
        sys.exit('No webhook configured. Add hukd: webhook_url: "..." to config.yaml')
    if args.test:
        ok = post(cfg['webhook_url'], {'username': 'HUKD Monitor',
                                       'content': f'✅ HUKD monitor test ({datetime.now():%d %b %H:%M})'})
        sys.exit(0 if ok else 'Test post failed — check the webhook URL.')
    lock = single_instance_lock()  # held for the life of the process
    if lock is None:
        sys.exit('The HUKD monitor is already running in the background. See hukd_monitor.log.')
    run(cfg, once=args.once)


if __name__ == '__main__':
    main()

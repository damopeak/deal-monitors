"""Currys -> Amazon flip monitor.

Sweeps every configured Currys category on a fixed cycle, recording each product's
price to a local state file. Keepa tokens are spent ONLY on products whose Currys
price actually dropped since the last sweep, which is what makes whole-site,
continuous coverage affordable:

    72 categories x ~20 products = ~1,440 products per sweep
    at 1 Keepa token per EAN that would be ~1,440 tokens = a full day's refill
    (budget is 1 token/min, bucket 60)

Gating on price drops turns that into dozens of lookups per sweep instead.

    python currys_monitor.py                 # run as a service
    python currys_monitor.py --once          # one sweep, then exit
    python currys_monitor.py --dry-run       # evaluate, post nothing, save nothing
    python currys_monitor.py --scrape-only   # sweep and diff, spend no Keepa tokens
"""
import argparse
import asyncio
import json
import logging
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from calculator import calculate_roi
from config_loader import load_config
from discord_notifier import DiscordNotifier
from image_match import IMAGE_MATCH_THRESHOLD, compare_images
from keepa_client import KeepaClient, _title_tokens
from scraper import Scraper

# ── Tunables ──────────────────────────────────────────────────────────────────
CYCLE_HOURS = 12.0         # how often a full sweep starts; this is your detection lag.
                           # 12 not 6: the sweep MUST run sequentially (see CONCURRENCY)
                           # and measures ~40s/page over ~520 pages = ~6.5h, plus up to
                           # ~2.3h of backfill. At 6h the cycles would overlap and leave
                           # no idle time for Keepa tokens to refill.
PAGES_PER_CATEGORY = 40    # 40 x 20 = 800, clears the largest category (Headphones, 651)
CONCURRENCY = 1            # MUST stay 1. At 4, Currys rate-limits the search-update-grid
                           # pagination XHR and categories silently stop at page 1 —
                           # measured 27 Sep: 3,720 of 9,667 products, 39 categories
                           # truncated (Headphones 22 of 651). Sequential gets 652/651.
MIN_ROI_PERCENT = 18.0     # owner's buying criteria — config.yaml's global min is 15
MIN_PROFIT = 2.00          # calculator.py has no profit floor, so it's applied here
MIN_MONTHLY_SALES = 10     # skip listings that barely move; matches the hukd: setting.
                           # Keepa's monthlySold, falling back to a BSR-drop estimate.
MIN_RANK_DROPS_90D = MIN_MONTHLY_SALES * 3   # when monthly sales are UNKNOWN, fall back to
                           # BSR rank drops (each drop implies a sale) over 90 days. Derived
                           # from MIN_MONTHLY_SALES so the unknown path demands the SAME rate
                           # as the known path — 10/month = 30 drops/90d — rather than being
                           # a soft back door into the alerts.
MIN_DROP_PERCENT = 5.0     # ignore trivial price wobble
CHECK_NEW_PRODUCTS = True  # also check products never seen before
MAX_NEW_PER_CYCLE = 250    # cap so a cold start doesn't try to check everything;
                           # apply_budget() clamps this to real token availability
TOKEN_RESERVE = 10         # leave this many Keepa tokens unspent
TOKENS_PER_CYCLE = 280     # ceiling for the post-sweep backfill only. Live per-category
                           # checking during the sweep is NOT capped by this — it spends
                           # whatever is currently in the bucket, which is what stops the
                           # 60-token ceiling silently discarding refills for ~7 hours.
TITLE_LOOKUP_TOKEN_COST = 16   # product_finder (11) + query of 5 ASINs (5)
MAX_TITLE_LOOKUPS_PER_CYCLE = 10   # ~160 tokens; only ~33% of EANs match, so this
                                   # is the fallback for the rest, biggest drops first
BACKFILL = True            # spend leftover budget working through the existing catalogue
                           # Superseded by live per-category checking (see
                           # make_category_handler), which spends tokens as they accrue
                           # during the sweep instead of in one blocking pass.
BACKFILL_MIN_PRICE = 15.0  # below this, 18% ROI AND £2 profit is near-impossible after
                           # FBA fees + £0.50 prep, so the token is wasted
BACKFILL_BAND = (20.0, 120.0)  # checked FIRST. Highest-price-first was tried on 27 Sep and
                           # returned 0 alerts from 200 products: clearing 18% ROI on a £900
                           # TV needs £162 profit, which effectively never happens, and
                           # high-value electronics are often gated. This band is where OA
                           # margins actually sit. Everything above MIN_PRICE still gets
                           # checked eventually — the band just goes first.
NO_MATCH_TTL_DAYS = 30     # don't re-ask Keepa about an EAN it didn't recognise
ALERT_COOLDOWN_HOURS = 168 # don't re-alert the same ASIN within a week
LOCK_PORT = 47834          # argos_monitor uses 47833

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, 'currys_state.json')
LOG_FILE = os.path.join(BASE_DIR, 'currys_monitor.log')
KEEPA_TOKEN_URL = 'https://api.keepa.com/token'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'),
              logging.StreamHandler()],
)
log = logging.getLogger('currys_monitor')


# ── state ─────────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault('prices', {})      # key  -> {price, title, url, seen}
    state.setdefault('no_match', {})    # ean  -> iso timestamp Keepa drew a blank
    state.setdefault('alerted', {})     # asin -> iso timestamp last alerted
    state.setdefault('asin_cache', {})  # ean  -> asin, so a product is only ever
                                        # identified once (see evaluate() tiers)
    state.setdefault('ean_checked', {}) # ean  -> iso; EAN lookup already attempted,
                                        # so the backfill never pays for it twice
    return state


def save_state(state: dict) -> None:
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def prune_state(state: dict) -> None:
    """Drop expired no-match and alert entries so the file doesn't grow forever."""
    now = _now()
    for ean, ts in list(state['no_match'].items()):
        seen = _parse(ts)
        if not seen or now - seen > timedelta(days=NO_MATCH_TTL_DAYS):
            del state['no_match'][ean]
    for asin, ts in list(state['alerted'].items()):
        seen = _parse(ts)
        if not seen or now - seen > timedelta(hours=ALERT_COOLDOWN_HOURS):
            del state['alerted'][asin]


# ── scrape ────────────────────────────────────────────────────────────────────

def build_currys_config(config: dict) -> dict:
    """A Currys-only copy of the config, forced enabled and headless."""
    currys = next((s for s in config['sites'] if s['name'] == 'Currys'), None)
    if currys is None:
        raise RuntimeError("No 'Currys' site block in config.yaml")

    cfg = dict(config)
    cfg['scraper'] = {**config['scraper'],
                      'max_pages_per_category': PAGES_PER_CATEGORY,
                      'concurrency': CONCURRENCY,
                      'headless': True}
    cfg['sites'] = [{**currys, 'enabled': True}]
    return cfg


def sweep(config: dict, on_category=None) -> list[dict]:
    cfg = build_currys_config(config)
    n_cats = len(cfg['sites'][0].get('categories', []))
    log.info(f"Sweep starting — {n_cats} categories, {PAGES_PER_CATEGORY} page(s) each")
    products = asyncio.run(Scraper(cfg).scrape_all_sites(on_category=on_category))
    log.info(f"Sweep complete — {len(products)} products")
    return products


def make_category_handler(config: dict, keepa: KeepaClient, notifier: DiscordNotifier,
                          state: dict, dry_run: bool, counter: dict):
    """Evaluate each category the moment it is scraped, rather than at the end.

    Two things fall out of this. Deals reach Discord throughout the sweep instead of
    in one burst hours later. And Keepa tokens get spent as they accrue: the bucket
    caps at 60, so during a ~7.3h sweep with no lookups everything above that ceiling
    was simply lost. Spending only what is currently held also means we never block
    waiting on refill, which is what pushed the pre-sweep pass over the cycle length.
    """
    def handle(site_name: str, cat_name: str, products: list[dict]) -> None:
        candidates = find_candidates(products, state)
        record_prices(products, state)          # after the diff, not before

        held = tokens_left(config['keepa_api_key'])
        budget = max(0, held - TOKEN_RESERVE)
        if budget <= 0:
            return

        # NB: no early return when there are no candidates. Once the catalogue is
        # baselined, "no drops, no new products" is the normal case — returning here
        # meant the backfill below never ran during a sweep at all.
        spent = 0
        to_check = drop_known_misses(candidates, state)[:budget] if candidates else []
        if to_check:
            log.info(f"[live] {cat_name}: checking {len(to_check)} ({held} tokens held)")
            alerts, spent = evaluate(to_check, config, keepa, notifier, state,
                                     dry_run, budget)
            counter['alerts'] += alerts

        # Spend whatever the drops didn't use on the standing catalogue. Without
        # this the bucket sits at its 60-token ceiling for the whole ~7h sweep and
        # every refill is thrown away — the backfill would only ever get the tail
        # end of a cycle.
        leftover = max(0, budget - spent)
        if BACKFILL and leftover > 0:
            counter['alerts'] += backfill(config, keepa, notifier, state,
                                          dry_run, leftover)

        if not dry_run:
            save_state(state)

    return handle


def product_key(p: dict) -> str:
    return p.get('ean') or p.get('url') or p.get('title', '')


# ── diff ──────────────────────────────────────────────────────────────────────

def find_candidates(products: list[dict], state: dict) -> list[dict]:
    """Products worth spending a Keepa token on: real price drops, then new items."""
    drops, new = [], []
    prices = state['prices']

    for p in products:
        key = product_key(p)
        if not key or not p.get('price'):
            continue

        prev = prices.get(key)
        if prev is None:
            if CHECK_NEW_PRODUCTS:
                new.append({**p, '_key': key, '_old': None, '_drop_pct': 0.0})
            continue

        old_price = prev.get('price')
        if not old_price or p['price'] >= old_price:
            continue

        drop_pct = (old_price - p['price']) / old_price * 100.0
        if drop_pct < MIN_DROP_PERCENT:
            continue
        drops.append({**p, '_key': key, '_old': old_price, '_drop_pct': drop_pct})

    drops.sort(key=lambda d: d['_drop_pct'], reverse=True)
    log.info(f"{len(drops)} price drops >= {MIN_DROP_PERCENT}%  |  {len(new)} new products")

    # Biggest drops first, then new products up to the cap
    return drops + new[:MAX_NEW_PER_CYCLE]


def record_prices(products: list[dict], state: dict) -> None:
    now = _iso(_now())
    for p in products:
        key = product_key(p)
        if not key or not p.get('price'):
            continue
        state['prices'][key] = {'price': p['price'],
                                'title': p.get('title', ''),
                                'url': p.get('url', ''),
                                'img_url': p.get('img_url', ''),
                                # kept so the backfill can rebuild a product dict
                                # that prices with the right category referral fee
                                'referral_fee_percent': p.get('referral_fee_percent'),
                                'referral_fee_tiers': p.get('referral_fee_tiers'),
                                'discount': p.get('discount', 0),
                                'discount_percent': p.get('discount_percent', 0),
                                'seen': now}


# ── Keepa budget ──────────────────────────────────────────────────────────────

def tokens_left(api_key: str) -> int:
    """Live token balance. keepa_client has no accounting of its own, so gate here."""
    try:
        r = requests.get(KEEPA_TOKEN_URL, params={'key': api_key}, timeout=30)
        return int(r.json().get('tokensLeft', 0))
    except Exception as e:
        log.warning(f"Could not read Keepa token balance ({e}) — assuming none spare")
        return 0


def drop_known_misses(candidates: list[dict], state: dict) -> list[dict]:
    """Skip products Keepa couldn't identify by either EAN or title."""
    fresh = [c for c in candidates if c.get('ean') not in state['no_match']]
    if len(candidates) - len(fresh):
        log.info(f"Skipped {len(candidates) - len(fresh)} products Keepa previously "
                 f"couldn't identify")
    return fresh


# ── evaluate + alert ──────────────────────────────────────────────────────────

def meets_criteria(deal: dict, amazon: dict) -> bool:
    """ROI and profit are measured against the 90-day-capped price (see keepa_client's
    spike guard), so a one-seller price hike can never clear this on its own."""
    if deal['roi'] < MIN_ROI_PERCENT or deal['profit'] < MIN_PROFIT:
        return False

    sold = amazon.get('monthly_sold')
    if sold is not None:
        return sold >= MIN_MONTHLY_SALES

    # Sales unknown — send it, but only with proof the listing actually sells.
    # rank_drops_90d is None when Keepa has no rank history at all, which means
    # there is no evidence it has ever sold.
    drops = amazon.get('rank_drops_90d')
    return drops is not None and drops >= MIN_RANK_DROPS_90D


def supplier_price(product: dict, config: dict) -> float:
    """Price actually paid at Currys: list price less category and brand discounts.

    Category discounts come from config.yaml (`discount` = flat £,
    `discount_percent` = % off, e.g. the 15%-off Windows laptop deal page).
    Brand rules in `brand_discounts` stack on top, matching main.py's behaviour.
    """
    price = product.get('price') or 0.0
    price -= product.get('discount', 0) or 0.0
    pct = product.get('discount_percent', 0) or 0.0
    if pct:
        price -= (product.get('price') or 0.0) * pct / 100.0

    title = (product.get('title') or '').lower()
    raw = product.get('price') or 0.0
    for rule in config.get('brand_discounts', []):
        keywords = rule.get('matches') or [rule.get('match', '')]
        if not all(kw.lower() in title for kw in keywords if kw):
            continue
        if raw < rule.get('min_price', 0):
            continue
        if rule['type'] == 'percent':
            price -= raw * rule['value'] / 100.0
        elif rule['type'] == 'fixed':
            price -= float(rule['value'])

    return round(max(0.0, price), 2)


def _process(product: dict, amazon: dict, config: dict, notifier: DiscordNotifier,
             state: dict, dry_run: bool) -> bool:
    """ROI-check one matched product and alert if it clears the criteria."""
    asin = amazon.get('asin', '')
    if asin and asin in state['alerted']:
        log.debug(f"[dedup] {asin} alerted recently — skipping")
        return False

    cost = supplier_price(product, config)
    if cost <= 0:
        return False
    deal = calculate_roi(cost, amazon, config, product)
    if not deal or not meets_criteria(deal, amazon):
        return False
    if cost < (product.get('price') or 0):
        deal['discount_applied'] = round(product['price'] - cost, 2)

    old = product.get('_old')
    drop = f"£{old:.2f} -> £{product['price']:.2f}" if old else 'new'
    via = amazon.get('match_type', 'ean')
    log.info(f"DEAL {product['title'][:45]!r} | {drop} | ROI {deal['roi']:.1f}% "
             f"| £{deal['profit']:.2f} | matched by {via}")

    if not dry_run:
        notifier.send_deal(product, amazon, deal)
        state['alerted'][asin] = _iso(_now())
    return True


def _title_score(product: dict, amazon: dict) -> float:
    """Word overlap (Jaccard) between the Currys and Amazon titles."""
    a = set(_title_tokens(product.get('title', '')))
    b = set(_title_tokens(amazon.get('title', '')))
    return len(a & b) / len(a | b) if a | b else 0.0


def evaluate(candidates: list[dict], config: dict, keepa: KeepaClient,
             notifier: DiscordNotifier, state: dict, dry_run: bool,
             budget: int) -> tuple[int, int]:
    """Match candidates to Amazon cheapest-route-first, within the token budget.

    Tier 1  cached ASIN  ->  1 token   (re-pricing something already identified)
    Tier 2  EAN lookup   ->  1 token   (only ~33% of Currys EANs match Keepa's)
    Tier 3  title search -> ~16 tokens (product_finder 11 + query 5) — capped hard,
                                        biggest price drops first
    A tier-2 or tier-3 hit caches ean->asin, so that product is tier 1 forever after.
    """
    if not candidates:
        return 0

    cache = state['asin_cache']
    alerts = 0
    spent = 0

    # ---- tier 1: products already resolved to an ASIN ------------------------
    cached = [c for c in candidates if c.get('ean') and c['ean'] in cache]
    asin_to_product = {cache[c['ean']]: c for c in cached}
    affordable = list(asin_to_product)[:max(0, budget - spent)]
    if affordable:
        log.info(f"[tier1] Re-pricing {len(affordable)} cached ASINs (1 token each)")
        for batch in keepa.lookup_products_by_asin_batched(affordable):
            for amazon in batch:
                product = asin_to_product.get(amazon.get('asin', ''))
                if product and _process(product, amazon, config, notifier, state, dry_run):
                    alerts += 1
        spent += len(affordable)

    # ---- tier 2: EAN lookup for everything not yet resolved ------------------
    uncached = [c for c in candidates if c.get('ean') and c['ean'] not in cache]
    by_ean = {}
    for c in uncached:
        by_ean.setdefault(c['ean'], c)

    eans = list(by_ean)[:max(0, budget - spent)]
    matched: set[str] = set()
    if eans:
        log.info(f"[tier2] EAN lookup for {len(eans)} products (1 token each)")
        for batch in keepa.lookup_products_by_ean_batched(eans):
            # One EAN can return several ASINs (e.g. the full-size and TKL versions of
            # a keyboard). Judge only the one whose title best matches Currys', rather
            # than all of them, which alerted on the wrong variant.
            best: dict[str, dict] = {}
            for amazon in batch:
                ean = amazon.get('ean')
                if ean in by_ean and (ean not in best or _title_score(by_ean[ean], amazon)
                                      > _title_score(by_ean[ean], best[ean])):
                    best[ean] = amazon
            for ean, amazon in best.items():
                product = by_ean[ean]
                matched.add(ean)
                if amazon.get('asin'):
                    cache[ean] = amazon['asin']
                if _process(product, amazon, config, notifier, state, dry_run):
                    alerts += 1
        spent += len(eans)
        now_iso = _iso(_now())
        for e in eans:
            state['ean_checked'][e] = now_iso

    # ---- tier 3: title search for EAN misses, biggest drops first ------------
    missed = [by_ean[e] for e in eans if e not in matched]
    missed.sort(key=lambda c: c.get('_drop_pct', 0.0), reverse=True)
    affordable_titles = min(
        MAX_TITLE_LOOKUPS_PER_CYCLE,
        max(0, (budget - spent)) // TITLE_LOOKUP_TOKEN_COST,
    )
    if missed:
        log.info(f"[tier3] {len(missed)} EAN misses; affording {affordable_titles} "
                 f"title searches at ~{TITLE_LOOKUP_TOKEN_COST} tokens each")

    now = _iso(_now())
    for product in missed[:affordable_titles]:
        amazon = keepa.lookup_by_title(product['title'])
        spent += TITLE_LOOKUP_TOKEN_COST
        if not amazon:
            state['no_match'][product['ean']] = now
            continue

        # Title matches are fuzzy — verify with the product image where possible
        supplier_img, amazon_img = product.get('img_url', ''), amazon.get('amazon_img', '')
        if supplier_img and amazon_img:
            score = compare_images(supplier_img, amazon_img)
            amazon['img_score'] = score
            if score < IMAGE_MATCH_THRESHOLD:
                log.info(f"[tier3] Rejected {product['title'][:40]!r} — image score "
                         f"{score:.2f} < {IMAGE_MATCH_THRESHOLD}")
                continue

        if amazon.get('asin'):
            cache[product['ean']] = amazon['asin']   # tier 1 from now on
        if _process(product, amazon, config, notifier, state, dry_run):
            alerts += 1

    # Anything we couldn't afford to title-search is left uncached, so it stays
    # a candidate next sweep rather than being written off as a miss.
    unchecked = len(missed) - min(len(missed), affordable_titles)
    if unchecked:
        log.info(f"[tier3] {unchecked} EAN misses deferred to a later sweep")

    log.info(f"[drops] Spent ~{spent} Keepa tokens")
    return alerts, spent


# ── backfill ──────────────────────────────────────────────────────────────────

def backfill(config: dict, keepa: KeepaClient, notifier: DiscordNotifier,
             state: dict, dry_run: bool, budget: int) -> int:
    """Work through products already on Currys, using whatever budget drops left over.

    EAN-only at 1 token each — title fallback (~16) is affordable for a specific
    price drop but not across ~9,500 products. Highest price first: a token spent
    on a £400 TV is far likelier to return a lead than one spent on a £15 kettle,
    and this ordering surfaces the real finds in the first days rather than weeks.
    Self-limiting: once the catalogue is covered there is nothing left to check and
    the whole budget reverts to drops.
    """
    if budget <= 0:
        return 0

    checked = state['ean_checked']
    todo = [
        {'ean': ean, **rec}
        for ean, rec in state['prices'].items()
        if ean not in checked
        and rec.get('price', 0) >= BACKFILL_MIN_PRICE
        and re.fullmatch(r'\d{12,13}', ean)   # price keys fall back to URL when EAN-less
    ]
    if not todo:
        log.info("[backfill] Catalogue fully checked — nothing left")
        return 0

    # Sweet-spot band first, cheapest within it: a given absolute margin is a much
    # bigger ROI% on a £30 item than a £300 one, and mid-priced goods are less often
    # gated. Anything outside the band is still checked, just later.
    lo, hi = BACKFILL_BAND
    todo.sort(key=lambda p: (0 if lo <= p['price'] <= hi else 1, p['price']))
    batch_products = todo[:budget]
    by_ean = {p['ean']: p for p in batch_products}

    in_band = sum(1 for p in batch_products if lo <= p['price'] <= hi)
    prices = [p['price'] for p in batch_products]
    log.info(f"[backfill] {len(todo)} products never checked; doing {len(batch_products)} "
             f"this cycle (£{min(prices):.2f}-£{max(prices):.2f}, {in_band} in the "
             f"£{lo:.0f}-{hi:.0f} band) — ~{len(todo) - len(batch_products)} will remain")

    alerts = 0
    for batch in keepa.lookup_products_by_ean_batched(list(by_ean)):
        for amazon in batch:
            ean = amazon.get('ean')
            product = by_ean.get(ean) if ean else None
            if not product:
                continue
            if amazon.get('asin'):
                state['asin_cache'][ean] = amazon['asin']
            if _process(product, amazon, config, notifier, state, dry_run):
                alerts += 1

    now_iso = _iso(_now())
    for ean in by_ean:
        checked[ean] = now_iso

    log.info(f"[backfill] Spent ~{len(by_ean)} tokens, {alerts} alerts")
    return alerts


# ── cycle ─────────────────────────────────────────────────────────────────────

def run_cycle(config: dict, dry_run: bool = False, scrape_only: bool = False) -> None:
    start = time.time()
    state = load_state()
    prune_state(state)

    if scrape_only:
        products = sweep(config)
        if not products:
            log.warning("Nothing scraped — blocked, or the markup changed. "
                        "Run test_currys_scrape.py to tell which.")
            return
        candidates = find_candidates(products, state)
        record_prices(products, state)
        if not dry_run:
            save_state(state)
        log.info(f"--scrape-only — {len(candidates)} candidates, no tokens spent")
        log.info(f"Cycle done — {len(products)} products, 0 alerts, "
                 f"{time.time() - start:.0f}s")
        return

    keepa = KeepaClient(config)
    notifier = DiscordNotifier(
        config,
        webhook_url=config.get('currys', {}).get('webhook_url'),
        username='Currys Monitor',
    )

    # Each category is diffed, priced and alerted on as soon as it is scraped, so
    # deals arrive throughout the sweep and tokens are spent as they accrue rather
    # than overflowing the 60-token bucket.
    counter = {'alerts': 0}
    handler = make_category_handler(config, keepa, notifier, state, dry_run, counter)

    log.info(f"Keepa: {tokens_left(config['keepa_api_key'])} tokens held at sweep start")
    products = sweep(config, on_category=handler)
    if not products:
        log.warning("Nothing scraped — blocked, or the markup changed. "
                    "Run test_currys_scrape.py to tell which.")
        return

    # Whatever accrued during the sweep that live checking didn't use goes to the
    # standing catalogue.
    if BACKFILL:
        held = tokens_left(config['keepa_api_key'])
        counter['alerts'] += backfill(config, keepa, notifier, state, dry_run,
                                      min(TOKENS_PER_CYCLE, max(0, held - TOKEN_RESERVE)))

    if not dry_run:
        save_state(state)

    remaining = sum(1 for e, r in state['prices'].items()
                    if e not in state['ean_checked']
                    and r.get('price', 0) >= BACKFILL_MIN_PRICE)
    log.info(f"Cycle done — {len(products)} products, {counter['alerts']} alerts, "
             f"{remaining} left to backfill, {time.time() - start:.0f}s")


# ── service ───────────────────────────────────────────────────────────────────

def single_instance_lock():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', LOCK_PORT))
    except OSError:
        return None
    return sock


def run_service(config: dict) -> None:
    log.info(f'Service started — sweeping every {CYCLE_HOURS}h')
    while True:
        started = time.time()
        try:
            run_cycle(config)
        except Exception:
            log.exception('Cycle crashed — retrying in 60s')
            time.sleep(60)
            continue

        wait = max(0.0, CYCLE_HOURS * 3600 - (time.time() - started))
        if wait:
            log.info(f'Next sweep in {wait / 60:.0f} min')
            time.sleep(wait)
        else:
            log.warning(f'Sweep took longer than the {CYCLE_HOURS}h cycle — '
                        f'starting the next one immediately')


def main() -> None:
    ap = argparse.ArgumentParser(description='Currys -> Amazon flips to Discord.')
    ap.add_argument('--once', action='store_true', help='run a single sweep and exit')
    ap.add_argument('--dry-run', action='store_true', help='evaluate, post nothing, save nothing')
    ap.add_argument('--scrape-only', action='store_true', help='sweep and diff, spend no tokens')
    args = ap.parse_args()

    # Held for every mode, not just the service: two concurrent sweeps would scrape
    # Currys twice over and race each other writing the state file.
    # Assigned to a local so the socket stays open for the life of the process.
    lock = single_instance_lock()          # noqa: F841
    if lock is None:
        log.error('Another Currys monitor is already running — exiting')
        sys.exit(1)

    config = load_config()
    if args.once or args.dry_run or args.scrape_only:
        run_cycle(config, dry_run=args.dry_run, scrape_only=args.scrape_only)
    else:
        run_service(config)


if __name__ == '__main__':
    main()

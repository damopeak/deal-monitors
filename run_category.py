"""Run the full pipeline (scrape → Keepa → ROI → Discord) for one category."""
import asyncio
import json
import logging
import os
import re
import time

from calculator import calculate_roi
from config_loader import load_config
from discord_notifier import DiscordNotifier
from image_match import compare_images, IMAGE_MATCH_THRESHOLD
from keepa_client import KeepaClient
from scraper import _dismiss_consent, USER_AGENT

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# ── Category to scan ──────────────────────────────────────────────────────────
CATEGORY_NAME = 'Windows Laptops'
BASE_URL = 'https://www.currys.co.uk/computing/laptops/laptops/windows-laptops'
REFERRAL_FEE_PERCENT = 7
CATEGORY_DISCOUNT = 100   # £100 off promotion
GIFT_CARD_DISCOUNT_PCT = 5  # additional % off via discounted gift cards
PAGE_SIZE = 20            # Currys page size (?sz=20)
MAX_PAGES = 20            # safety cap
SENT_DEALS_FILE = os.path.join(os.path.dirname(__file__), 'sent_deals.json')


# ── Scrape all pages directly by URL ─────────────────────────────────────────

# Signature of the product grid, used to tell when the AJAX page swap has landed.
# data-productdatalayer rather than data-pid: data-pid also covers carousel tiles,
# which shuffle on their own and give a false "page changed" reading.
GRID_SIG = """() => Array.from(document.querySelectorAll('[data-productdatalayer]'))
    .map(el => el.getAttribute('data-productdatalayer').slice(-40)).join(',')"""


async def scrape_all_pages(config: dict) -> list[dict]:
    """Page through a Currys category by clicking Next.

    Do NOT navigate to `?start=N&sz=20` URLs — Currys now answers those with
    Cloudflare "Attention Required", proxy or not. Pagination has to be driven by
    clicking `a.page-next`, which fires the search-update-grid XHR in-page. That
    XHR is itself 403'd unless navigator.webdriver is masked, hence the init
    script below.
    """
    from playwright.async_api import async_playwright

    all_products = []
    seen_urls: set = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, channel='chrome')
        context = await browser.new_context(user_agent=USER_AGENT)
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = await context.new_page()

        logger.info(f"[scraper] Page 1: {BASE_URL}")
        await page.goto(BASE_URL, wait_until='domcontentloaded', timeout=45_000)
        await asyncio.sleep(4)
        await _dismiss_consent(page)
        await asyncio.sleep(3)

        for page_num in range(1, MAX_PAGES + 1):
            page_products = await extract_products(page)
            new = [p for p in page_products if p['url'] not in seen_urls]
            if not new:
                logger.info(f"[scraper] Page {page_num}: no new products — done")
                break

            for p in new:
                seen_urls.add(p['url'])
            all_products.extend(new)
            logger.info(f"[scraper] Page {page_num}: {len(new)} new products "
                        f"(total {len(all_products)})")

            sig = await page.evaluate(GRID_SIG)
            try:
                link = page.locator('a.page-next, a.updateGrid.page-next').first
                if not await link.is_visible(timeout=3000):
                    logger.info("[scraper] No next page link — done with category")
                    break
                # Must be in view or the click doesn't fire the XHR
                await link.scroll_into_view_if_needed(timeout=5000)
                await asyncio.sleep(1)
                await link.click()
            except Exception as e:
                logger.info(f"[scraper] Pagination stopped ({type(e).__name__}) — done")
                break

            for _ in range(20):
                await asyncio.sleep(1)
                if await page.evaluate(GRID_SIG) != sig:
                    break
            else:
                logger.warning("[scraper] Grid never changed after clicking Next — "
                               "stopping (pagination may be blocked)")
                break

            await asyncio.sleep(2)   # let the new grid settle before extracting

        await browser.close()

    return all_products


async def extract_products_from_html(html: str) -> list[dict]:
    """Parse product datalayers from a raw HTML fragment (search-update-grid response)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'lxml')
    items = []
    seen = set()
    for el in soup.select('[data-productdatalayer]'):
        raw = el.get('data-productdatalayer', '')
        if not raw:
            continue
        key = raw[-40:]
        if key in seen:
            continue
        seen.add(key)
        link = el.select_one('a.pdpLink, a[href*="/products/"]')
        url = link['href'] if link else ''
        if url and not url.startswith('http'):
            url = 'https://www.currys.co.uk' + url
        img = el.select_one('img')
        img_url = ''
        if img:
            img_url = (img.get('data-src') or img.get('data-lazy-src')
                       or img.get('data-original') or img.get('src') or '')
            if img_url.startswith('data:') or 'placeholder' in img_url:
                img_url = ''
        items.append({'url': url, 'datalayer_raw': raw, 'imgUrl': img_url})
    return _parse_raw_items(items)


def _parse_raw_items(raw_items: list[dict]) -> list[dict]:
    products = []
    for item_raw in raw_items:
        if not item_raw.get('datalayer_raw'):
            continue
        try:
            dl = json.loads(item_raw['datalayer_raw'])
        except Exception:
            continue
        if not isinstance(dl, list) or not dl:
            continue
        item = dl[0]
        title = item.get('name', '').strip()
        if not title:
            continue
        ean_raw = item.get('ean')
        if isinstance(ean_raw, list):
            ean = str(ean_raw[0]) if ean_raw else None
        elif ean_raw:
            ean = str(ean_raw)
        else:
            ean = None
        if ean and not re.fullmatch(r'\d{12,13}', ean):
            ean = None
        price = None
        for avail in item.get('availability', []):
            for shipping in avail.get('shipping', []):
                for pay in shipping.get('payment', []):
                    if pay.get('frequency') == 'one off' and pay.get('installments', 0) == 1:
                        amt = pay.get('amount')
                        if amt and float(amt) > 0:
                            price = float(amt)
                            break
                if price:
                    break
            if price:
                break
        if not price:
            continue
        products.append({
            'title': title,
            'price': price,
            'ean': ean,
            'url': item_raw['url'],
            'img_url': item_raw.get('imgUrl', '') or item_raw.get('img_url', ''),
            'site_name': 'Currys',
            'referral_fee_percent': REFERRAL_FEE_PERCENT,
            'referral_fee_tiers': None,
            'discount': CATEGORY_DISCOUNT,
        })
    return products


async def extract_products(page) -> list[dict]:
    raw_items = await page.evaluate("""() => {
        const dlEls = document.querySelectorAll('[data-productdatalayer]');
        const results = [];
        const seen = new Set();
        for (const el of dlEls) {
            const datalayer_raw = el.getAttribute('data-productdatalayer');
            if (!datalayer_raw) continue;
            const key = datalayer_raw.substring(datalayer_raw.length - 40);
            if (seen.has(key)) continue;
            seen.add(key);
            const container = el.closest('li, article, [class*="product"], [class*="tile"]') || el;
            const linkEl = container.querySelector('a.pdpLink, a[href*="/products/"]');
            const url = linkEl ? linkEl.href : '';
            const imgEl = container.querySelector('img');
            let imgUrl = '';
            if (imgEl) {
                imgUrl = imgEl.getAttribute('data-src') || imgEl.getAttribute('data-lazy-src')
                       || imgEl.getAttribute('data-original') || imgEl.getAttribute('src') || '';
                if (imgUrl.startsWith('data:') || imgUrl.includes('placeholder')) imgUrl = '';
            }
            results.push({ url, datalayer_raw, imgUrl });
        }
        return results;
    }""")
    return _parse_raw_items(raw_items)


def _brand_discount(product: dict, config: dict) -> float:
    raw_price = product['price']
    title = product.get('title', '').lower()
    extra = 0.0
    for rule in config.get('brand_discounts', []):
        keywords = rule.get('matches') or [rule['match']]
        if not all(kw.lower() in title for kw in keywords):
            continue
        if raw_price < rule.get('min_price', 0):
            continue
        if rule['type'] == 'percent':
            extra += raw_price * rule['value'] / 100.0
        elif rule['type'] == 'fixed':
            extra += float(rule['value'])
    return extra


def _load_sent_deals() -> set:
    try:
        with open(SENT_DEALS_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_sent_deals(sent: set):
    with open(SENT_DEALS_FILE, 'w') as f:
        json.dump(list(sent), f)


def _already_sent(asin: str, sent: set) -> bool:
    return asin in sent


def _process_deal(product: dict, amazon_data: dict, config: dict, notifier) -> bool:
    flat_discount = product.get('discount', 0)
    price_after_flat = product['price'] - flat_discount
    if price_after_flat <= 0:
        return False
    gift_card_saving = round(price_after_flat * GIFT_CARD_DISCOUNT_PCT / 100, 2)
    supplier_price = price_after_flat - gift_card_saving
    discount = flat_discount + gift_card_saving
    if supplier_price <= 0:
        return False
    deal = calculate_roi(supplier_price, amazon_data, config, product)
    if deal and discount:
        deal['discount_applied'] = discount
    if not deal:
        return False
    logger.info(
        f"DEAL: {product['title'][:50]!r} | "
        f"ROI {deal['roi']:.1f}% | Profit £{deal['profit']:.2f}"
    )
    notifier.send_deal(product, amazon_data, deal)
    return True


def main():
    config = load_config()
    keepa = KeepaClient(config)
    notifier = DiscordNotifier(config)

    start = time.time()
    logger.info(f"Starting: {CATEGORY_NAME}")

    products = asyncio.run(scrape_all_pages(config))
    logger.info(f"Scraped {len(products)} products total")

    with_ean = [p for p in products if p.get('ean')]
    without_ean = [p for p in products if not p.get('ean')]
    logger.info(f"{len(with_ean)} with EAN | {len(without_ean)} without EAN")

    eans = [p['ean'] for p in with_ean]
    ean_to_product: dict = {}
    for p in with_ean:
        ean_to_product.setdefault(p['ean'], p)

    matched_eans: set = set()
    deals_count = 0
    sent_deals = _load_sent_deals()

    # Phase 1 — EAN batch lookup
    for batch_results in keepa.lookup_products_by_ean_batched(eans):
        for amazon_data in batch_results:
            ean = amazon_data.get('ean')
            if not ean:
                continue
            matched_eans.add(ean)
            product = ean_to_product.get(ean)
            if not product:
                logger.debug(f"[ean] Keepa returned EAN {ean} not in product map — skipping")
                continue
            asin = amazon_data.get('asin', '')
            if _already_sent(asin, sent_deals):
                logger.debug(f"[dedup] Skipping {asin} — already sent")
                continue
            if _process_deal(product, amazon_data, config, notifier):
                deals_count += 1
                sent_deals.add(asin)
                _save_sent_deals(sent_deals)

    # Phase 2 — title fallback for:
    #   a) EAN products whose EAN didn't match anything in Keepa's eanList
    #      (Currys and Amazon sometimes use different EAN variants for the same product)
    #   b) Genuinely EAN-less products
    unmatched_ean = [p for p in with_ean if p['ean'] not in matched_eans]
    needs_title = unmatched_ean + without_ean
    logger.info(
        f"[title] {len(needs_title)} products for title fallback "
        f"({len(unmatched_ean)} unmatched EAN + {len(without_ean)} EAN-less)"
    )
    unsure_queue: list[tuple[dict, dict]] = []

    for product in needs_title:
        amazon_data = keepa.lookup_by_title(product['title'])
        if not amazon_data:
            continue
        asin = amazon_data.get('asin', '')
        if _already_sent(asin, sent_deals):
            logger.debug(f"[dedup] Skipping {asin} — already sent")
            continue
        supplier_img = product.get('img_url', '')
        amazon_img = amazon_data.get('amazon_img', '')
        if supplier_img and amazon_img:
            score = compare_images(supplier_img, amazon_img)
            amazon_data['img_score'] = score
            logger.info(
                f"[image] {product['title'][:40]!r} → score={score:.2f} "
                f"({'✓' if score >= IMAGE_MATCH_THRESHOLD else '✗'})"
            )
        if amazon_data.get('match_unsure'):
            unsure_queue.append((product, amazon_data))
        else:
            if _process_deal(product, amazon_data, config, notifier):
                deals_count += 1
                sent_deals.add(asin)
                _save_sent_deals(sent_deals)

    # Unsure matches sent after all confident ones
    if unsure_queue:
        logger.info(f"[title] Sending {len(unsure_queue)} unsure matches")
    for product, amazon_data in unsure_queue:
        asin = amazon_data.get('asin', '')
        if _already_sent(asin, sent_deals):
            continue
        if _process_deal(product, amazon_data, config, notifier):
            deals_count += 1
            sent_deals.add(asin)
            _save_sent_deals(sent_deals)

    elapsed = time.time() - start
    logger.info(f"Done — {deals_count} deals in {elapsed:.0f}s")
    notifier.send_run_summary(len(products), deals_count, elapsed)


if __name__ == '__main__':
    run_number = 0
    while True:
        run_number += 1
        logger.info(f"=== Run #{run_number} starting ===")
        try:
            main()
        except Exception as e:
            logger.error(f"Run #{run_number} crashed: {e}", exc_info=True)
        logger.info(f"=== Run #{run_number} complete — restarting in 10 minutes ===")
        time.sleep(600)

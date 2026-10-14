import asyncio
import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler

from calculator import calculate_roi
from config_loader import load_config
from discord_notifier import DiscordNotifier
from keepa_client import KeepaClient
from scraper import Scraper

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def _brand_discount(product: dict, config: dict) -> float:
    """
    Return additional discount (£) from brand_discounts rules in config.
    Each matching rule adds to the total — rules are independent.
    """
    raw_price = product['price']
    title = product.get('title', '').lower()
    extra = 0.0

    for rule in config.get('brand_discounts', []):
        # Single keyword or list of keywords (all must match)
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


def run_bot(config: dict):
    start = time.time()
    logger.info("=" * 60)
    logger.info("OA Bot run started")
    logger.info("=" * 60)

    scraper = Scraper(config)
    keepa = KeepaClient(config)
    notifier = DiscordNotifier(config)

    # ------------------------------------------------------------------ #
    # 1. Scrape all configured sites
    # ------------------------------------------------------------------ #
    logger.info("[main] Starting scrape...")
    products = asyncio.run(scraper.scrape_all_sites())
    logger.info(f"[main] Scraped {len(products)} products total")

    # ------------------------------------------------------------------ #
    # 2. Split by whether we have an EAN
    # ------------------------------------------------------------------ #
    with_ean = [p for p in products if p.get('ean')]
    without_ean = [p for p in products if not p.get('ean')]
    logger.info(f"[main] {len(with_ean)} with EAN  |  {len(without_ean)} without EAN (title-match skipped for now)")

    # ------------------------------------------------------------------ #
    # 3 & 4. Keepa EAN lookup + ROI — send deals as each batch arrives
    # ------------------------------------------------------------------ #
    eans = [p['ean'] for p in with_ean]

    # Build a lookup map from EAN → supplier product for fast access
    ean_to_product: dict = {}
    for p in with_ean:
        ean_to_product.setdefault(p['ean'], p)

    deals_count = 0

    for batch_results in keepa.lookup_products_by_ean_batched(eans):
        logger.info(f"[main] Keepa batch returned {len(batch_results)} matched products — processing now")

        for amazon_data in batch_results:
            ean = amazon_data.get('ean')
            if not ean:
                continue
            product = ean_to_product.get(ean)
            if not product:
                continue

            cat_discount = product.get('discount', 0)
            brand_disc = _brand_discount(product, config)
            discount = cat_discount + brand_disc
            supplier_price = product['price'] - discount
            if supplier_price <= 0:
                continue

            deal = calculate_roi(supplier_price, amazon_data, config, product)
            if deal and discount:
                deal['discount_applied'] = discount
            if not deal:
                continue

            deals_count += 1
            logger.info(
                f"[main] DEAL: {product['title'][:50]!r}  |  "
                f"ROI {deal['roi']:.1f}%  |  Profit £{deal['profit']:.2f}"
            )
            notifier.send_deal(product, amazon_data, deal)

    # ------------------------------------------------------------------ #
    # 5. Run summary
    # ------------------------------------------------------------------ #
    elapsed = time.time() - start
    logger.info(f"[main] Run complete — {deals_count} deals in {elapsed:.0f}s")
    notifier.send_run_summary(len(products), deals_count, elapsed)


def main():
    config = load_config()

    # Run once immediately on startup
    run_bot(config)

    # Then schedule every 4 hours
    scheduler = BlockingScheduler()
    scheduler.add_job(run_bot, 'interval', hours=4, args=[config], id='oa_bot')
    logger.info("[main] Scheduler running — next run in 4 hours. Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("[main] Shutdown requested.")


if __name__ == '__main__':
    main()

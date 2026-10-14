"""Scrape-only test for the Currys site config — no Keepa, no Discord.

Runs the real Scraper against a couple of Currys categories and reports how much
usable data comes back (titles, prices, EANs). Use this to tell a markup change
apart from a proxy/blocking problem.

    python test_currys_scrape.py              # direct connection
    python test_currys_scrape.py --proxy      # via the configured Oxylabs proxy
"""
import asyncio
import logging
import sys

from config_loader import load_config
from scraper import Scraper

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

TEST_CATEGORIES = ['Air Fryers', 'Headphones']


def main():
    use_proxy = '--proxy' in sys.argv
    config = load_config()

    currys = next(s for s in config['sites'] if s['name'] == 'Currys')
    cats = [c for c in currys['categories'] if c.get('name') in TEST_CATEGORIES]

    # Only Currys, only the test categories, enabled regardless of config.yaml
    config['sites'] = [{**currys,
                        'enabled': True,
                        'use_proxy': use_proxy,
                        'categories': cats}]
    if not use_proxy:
        config['scraper']['proxy'] = None

    print(f"\nTesting {len(cats)} categories | proxy: {'ON' if use_proxy else 'OFF'}\n")

    products = asyncio.run(Scraper(config).scrape_all_sites())

    print("\n" + "=" * 70)
    print(f"TOTAL PRODUCTS: {len(products)}")
    if not products:
        print("Nothing scraped — blocked, or the product markup has changed.")
        print("=" * 70)
        return

    with_ean = [p for p in products if p.get('ean')]
    with_price = [p for p in products if p.get('price')]
    print(f"  with price : {len(with_price)}/{len(products)}")
    print(f"  with EAN   : {len(with_ean)}/{len(products)}")
    print("\nSample:")
    for p in products[:8]:
        print(f"  £{p.get('price', 0):>8.2f} | EAN {p.get('ean') or '-':<14} | "
              f"{p.get('title', '')[:58]}")
    print("=" * 70)


if __name__ == '__main__':
    main()

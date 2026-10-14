import asyncio, sys
sys.path.insert(0, '.')
from run_category import scrape_all_pages
from config_loader import load_config

config = load_config()
products = asyncio.run(scrape_all_pages(config))
print(f"\nTotal: {len(products)} products")
for p in products[:5]:
    print(f"  {p['title'][:60]} | £{p['price']} | EAN:{p['ean']}")

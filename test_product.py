"""One-shot test: look up a single product by EAN and send to Discord."""
import logging
from calculator import calculate_roi
from config_loader import load_config
from discord_notifier import DiscordNotifier
from keepa_client import KeepaClient

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

EAN = "0197105935396"
BUY_PRICE = 450.0
PAGE_URL = "https://www.currys.co.uk/products/asus-vivobook-s14-m3407ha-14-laptop-amd-ryzen-7-1-tb-ssd-silver-10286936.html"
REFERRAL_FEE_PERCENT = 7  # computing / laptops

config = load_config()
keepa = KeepaClient(config)
notifier = DiscordNotifier(config)

logger.info(f"Looking up EAN {EAN} on Keepa...")
results = keepa.lookup_products_by_ean([EAN])

if not results:
    logger.error("No Keepa match found for this EAN.")
else:
    amazon_data = results[0]
    logger.info(f"Keepa match: {amazon_data['title'][:60]!r}  ASIN={amazon_data['asin']}")
    logger.info(f"Selling price: £{amazon_data['selling_price']:.2f} ({amazon_data['price_type']})")
    logger.info(f"FBA fee: £{amazon_data['fba_fee']:.2f}  BSR: {amazon_data.get('bsr')}")

    product = {
        'title': amazon_data['title'],
        'price': BUY_PRICE,
        'ean': EAN,
        'url': PAGE_URL,
        'referral_fee_percent': REFERRAL_FEE_PERCENT,
    }

    deal = calculate_roi(BUY_PRICE, amazon_data, config, product)

    if not deal:
        logger.info("ROI below threshold — no deal. Sending to Discord anyway for test.")
        deal = {
            'profit': 0.0,
            'roi': 0.0,
            'referral_fee': amazon_data['selling_price'] * REFERRAL_FEE_PERCENT / 100,
            'fba_fee': amazon_data['fba_fee'],
            'digital_services_fee': 0.0,
            'prep_fee': 0.5,
            'selling_price': amazon_data['selling_price'],
            'supplier_price': BUY_PRICE,
            'price_type': amazon_data['price_type'],
        }

    notifier.send_deal(product, amazon_data, deal)
    logger.info("Sent to Discord.")

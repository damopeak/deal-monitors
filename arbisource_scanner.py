"""Scheduled ArbiSource store scanning.

ArbiSource has no scheduled-scan feature of its own — the store scan builder offers
only Store Cache / Amazon Cache / Filters / Exclude / START SCAN, and the create
payload hardcodes isAutoScan:false. So the cycle is driven from here.

Every run: check the queue has room, start a scan per store covering ALL categories
with no server-side filters, then let arbisource_results.py judge what comes back.
Selecting all categories costs nothing extra per scan and our own filter is what
stops the noise.

    python arbisource_scanner.py --once --stores Costco     # one store, verify it works
    python arbisource_scanner.py --once                     # all configured stores
    python arbisource_scanner.py                            # every 6 hours
"""
import argparse
import logging
import sys
import time

from dotenv import load_dotenv

from arbisource_client import ArbiSourceClient, ArbiSourceError
from arbisource_monitor import load_config

load_dotenv()
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler("arbisource_scanner.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("arbisource.scanner")

# Cache windows in DAYS. Premium allows a 2h store minimum and no Amazon minimum.
STORE_CACHE_DAYS = 2 / 24.0     # 2 hours
AMAZON_CACHE_DAYS = 6 / 24.0    # 6 hours
QUEUE_LIMIT = 50
CYCLE_SECONDS = 6 * 60 * 60

STORES = [
    "Costco", "AO", "Laptops Direct", "Smyths Toys", "HMV", "Currys",
    "Currys Business", "Argos", "Very", "B&Q", "Overclockers UK",
    "CCL Online", "Chaos Cards",
]


def queue_depth(client):
    return len([s for s in client.scans(only_completed=False)
                if not s.get("completedDateTime")])


def start_store_scan(client, store, categories, region="UK"):
    body = {
        "emailOnCompletion": False,
        "storeCache": STORE_CACHE_DAYS,
        "amazonCache": AMAZON_CACHE_DAYS,
        "isAutoScan": False,
        "region": region,
        "filterSettings": client.wide_filters(),
        "filterOption": "none",
        "checkRestrictions": True,
        "excludedKeywords": [],
        "selectedStoresAndCategories": {store: categories},
        "storeDiscounts": {},
        "storePriceOverrides": {},
        "customCategoryLinks": {},
    }
    return client._call("POST", "/scan/storeScan", json=body)


def run_cycle(client, stores, dry_run=False):
    all_stores = client._call("GET", "/store/getAllStores")
    depth = queue_depth(client)
    logger.info("queue %d/%d before starting", depth, QUEUE_LIMIT)

    started = 0
    for store in stores:
        cfgs = all_stores.get(store)
        if not cfgs:
            logger.warning("%s not in ArbiSource store list — skipping", store)
            continue
        cats = cfgs.get("categoryNames") or []
        if not cats:
            logger.warning("%s has no categories — skipping", store)
            continue
        if depth + started >= QUEUE_LIMIT:
            logger.warning("queue full (%d), stopping after %d scans", QUEUE_LIMIT, started)
            break
        if dry_run:
            logger.info("[dry-run] would scan %s (%d categories)", store, len(cats))
            continue
        try:
            start_store_scan(client, store, cats)
            started += 1
            logger.info("started %s — %d categories", store, len(cats))
            time.sleep(2)
        except ArbiSourceError as e:
            logger.error("failed to start %s: %s", store, str(e)[:200])
    logger.info("cycle done — %d scans started", started)
    return started


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stores", nargs="*", default=None)
    args = ap.parse_args()

    cfg = load_config()
    client = ArbiSourceClient(cost_overrides=cfg.get("costs"))
    stores = args.stores or STORES

    if args.once:
        run_cycle(client, stores, args.dry_run)
        return

    while True:
        try:
            run_cycle(client, stores, args.dry_run)
        except Exception as e:
            logger.exception("cycle failed: %s", e)
        logger.info("sleeping %d hours", CYCLE_SECONDS // 3600)
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()

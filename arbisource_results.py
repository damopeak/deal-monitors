"""Score the results of completed ArbiSource scans and post the good ones to Discord.

Scans massively outproduce the price-drop feed (a single Costco scan returned 393
"profitable" results against ~5 per 400 price drops) and the results are private to
the account rather than shared with every subscriber. Same filter, bigger input.

Scan results use slightly different field names from price drops, so they are
normalised to the price-drop shape before filtering.

    python arbisource_results.py --dry-run       # score everything, post nothing
    python arbisource_results.py                 # post qualifying results
    python arbisource_results.py --stores Costco Argos
"""
import argparse
import json
import logging
import os
import sys
import time

from dotenv import load_dotenv

from arbisource_client import ArbiSourceClient
from arbisource_monitor import Seen, load_config, post
import arbisource_filter as af

load_dotenv()
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler("arbisource_results.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("arbisource.results")

SEEN_FILE = "arbisource_results_seen.json"

# The stores worth re-scanning on a cycle.
DEFAULT_STORES = [
    "Costco", "AO", "Laptops Direct", "Smyths Toys", "HMV", "Currys",
    "Currys Business", "Argos", "Very", "B&Q", "Overclockers UK",
    "CCL Online", "Chaos Cards",
]


def normalise(r):
    """Map a scan result onto the price-drop shape the filter expects."""
    d = dict(r)
    d.setdefault("stockStatus", r.get("storeStockStatus"))
    # Scan results carry no was/now pair — the price is simply the price.
    if not d.get("storeDiscountedPrice"):
        d["storeDiscountedPrice"] = r.get("storePrice")
    if not d.get("storeDiscountedPriceExclVAT"):
        d["storeDiscountedPriceExclVAT"] = r.get("storePriceExclVAT")
    d.setdefault("reduction", 0)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stores", nargs="*", default=None)
    ap.add_argument("--per-scan", type=int, default=400,
                    help="max results to pull per scan")
    ap.add_argument("--max-posts", type=int, default=15)
    ap.add_argument("--prime", action="store_true",
                    help="mark everything seen without posting")
    args = ap.parse_args()

    cfg = load_config()
    stores = set(args.stores or DEFAULT_STORES)
    client = ArbiSourceClient(cost_overrides=cfg.get("costs"))
    seen = Seen(SEEN_FILE)
    wide = client.wide_filters()

    scans = client.scans()
    logger.info("%d completed scans; filtering results to %d stores", len(scans), len(stores))

    candidates, examined = [], 0
    for s in scans:
        got, skip = [], 0
        while skip < args.per_scan:
            batch = client.get_scan_results(s["_id"], skip=skip, per_page=80,
                                            filter_settings=wide)
            if not batch:
                break
            got += batch
            skip += len(batch)
            if len(batch) < 80:
                break
        if not got:
            continue
        examined += len(got)
        for raw in got:
            if stores and raw.get("storeName") not in stores:
                continue
            key = str(raw.get("resultID") or raw.get("_id"))
            if key in seen:
                continue
            seen.add(key)
            d = normalise(raw)
            v = af.evaluate(d, cfg["filter"], cfg["vat_registered"])
            if v["pass"]:
                candidates.append((v, d))

    candidates.sort(key=lambda t: -t[0]["score"])
    logger.info("examined %d results -> %d qualify", examined, len(candidates))

    for v, d in candidates[:args.max_posts]:
        m = v["metrics"]
        logger.info("PASS [%s %.0f] %s | ROI@90d %.0f%% £%.2f | %d dr/30d | £%.2f @ %s",
                    v["tier"], v["score"], (d.get("storeProductName") or "")[:48],
                    m["roi_90"], m["profit_90"], m["rank_drops_30"],
                    m["cash_price"], d.get("storeName"))
        if not args.dry_run and not args.prime:
            post(cfg["webhook_url"], d, v, cfg["ping_on_elite"])
            time.sleep(1.2)

    if len(candidates) > args.max_posts:
        logger.info("%d more qualified but were held back", len(candidates) - args.max_posts)
    seen.save()


if __name__ == "__main__":
    main()

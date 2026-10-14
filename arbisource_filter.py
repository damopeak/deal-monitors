"""Quality filter for ArbiSource price drops.

ArbiSource's own "matched" filter is far too loose to notify on — it was letting
through ~1,100 of ~10,500 drops. This layer is what decides whether a drop is
worth a Discord ping.

The central idea: a price drop's headline ROI is computed against the *current*
Amazon price, which is often a spike. What matters is whether the deal still works
at the price the listing actually trades at and whether the item sells at all.
So every judgement here is made against the 90-day average selling price and the
30-day rank-drop history, not the spot figures.
"""
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEFAULTS = {
    # --- hard gates ---
    "min_roi": 20.0,             # % at the 90-day average selling price
    "min_profit": 4.00,          # £ at the 90-day average selling price
    "max_cost": 250.0,           # £ per unit, matches the account's costBoundaries
    "inbound_per_unit": 0.0,     # flat £/unit to get stock into Amazon (applied locally)
    "min_profit_spot": 2.00,     # £ at today's price — stops "only works in theory" deals
    "max_bsr_percent": 1.0,      # % of category — only binds when velocity is weak too
    "hard_max_bsr_percent": 5.0, # absolute ceiling regardless of velocity
    "min_rank_drops_30": 15,     # per variation — roughly "sells every other day"
    "divide_drops_by_variations": True,  # False: count rank drops across the whole listing
    "max_sellers": 25,
    "min_confidence": 0.75,      # product-match confidence when there is no EAN match
    "ean_min_corroboration": 0.30,  # even an EAN match needs some title/image agreement
    "max_keepa_age_days": 120,   # stale Keepa data makes every sales figure meaningless
    "spike_tolerance": 1.20,     # current price may sit at most 20% above the 90d average
    "decline_tolerance": 0.90,   # 30d average must hold >=90% of the 90d average
    "require_in_stock": False,   # let these through, but flagged and score-penalised
    "require_green_ip": False,   # same — ArbiSource's IP flag is conservative
    # Bulky white goods: not wanted and the worst offenders for scrape errors.
    # Amazon's own categories do the real work here — far more reliable than keywords.
    # "Large Appliances" is white goods; the media ones cover games, CDs and DVDs.
    "excluded_categories": [
        "Large Appliances",
        "PC & Video Games", "Video Games",
        "CDs & Vinyl", "Music", "DVD & Blu-ray", "Movies & TV",
    ],
    # Matched on word boundaries and kept deliberately specific — a bare "cooker" or
    # "oven" catches countertop kit like the Ninja Speedi ("Rapid Cooker & Air Fryer"),
    # which is not what "big electricals" means.
    "excluded_keywords": [
        "fridge", "freezer", "dishwasher", "washing machine", "washer dryer",
        "tumble dryer", "range cooker", "cooker hood", "extractor hood",
        "electric hob", "gas hob", "induction hob", "built-in oven", "built in oven",
        "electric cooker", "gas cooker", "range oven", "chest freezer",
        # Console games / discs — the category check catches most, these catch the rest
        # where a retailer files them oddly.
        "playstation", "xbox", "nintendo switch", "ps5", "ps4",
        "blu-ray", "blu ray", "dvd", "vinyl lp",
    ],
    # Rescues a product from an excluded CATEGORY when the title says it is really an
    # accessory rather than a game or a disc.
    "category_allow_keywords": [
        "keyboard", "mouse", "mousepad", "mouse mat", "headset", "headphones",
        "controller", "monitor", "webcam", "microphone", "chair", "desk",
        "ssd", "hard drive", "graphics card", "gpu", "ram", "cable", "hub",
        "dock", "docking", "speaker", "router", "capture card",
    ],
    # A price that collapses this far in one step is usually a mis-scrape, not a deal.
    "suspect_discount": 80.0,
    "suspect_discount_blocks_ping": True,
    # Power tools and anything with a lithium cell get flagged as dangerous goods and
    # some of the best tool margins sit there. Flagged and penalised, not binned.
    "exclude_hazmat": False,
    "penalty_hazmat": 0.0,
    "exclude_oversized": False,
    "exclude_amazon_on_listing": True,
    # Applied when the gate above is off, so a flagged deal still ranks below a clean
    # one of equal economics. Set to 0 to treat them identically.
    "penalty_out_of_stock": 15.0,
    "penalty_ip_flag": 0.0,
    # --- notification tiers, by score ---
    "tier_elite": 65,
    "tier_strong": 35,
}


def _num(v, default=0.0):
    try:
        if isinstance(v, dict):
            v = v.get("listingPrice", default)
        return float(v)
    except (TypeError, ValueError):
        return default


# Model codes are the most reliable mismatch signal there is: "PC201UK" and "MO201UK"
# look alike but are a pressure cooker and a pizza oven. Requires >=2 letters and >=2
# digits so sizes ("6L") and pack counts are ignored.
_MODEL_RE = re.compile(r"\b(?=[A-Z0-9-]*[A-Z]{2})(?=[A-Z0-9-]*[0-9]{2})[A-Z][A-Z0-9-]{3,}\b")


def _model_codes(name):
    if not name:
        return set()
    return {c.replace("-", "") for c in _MODEL_RE.findall(str(name).upper())}


def _keepa_age_days(drop):
    ts = (drop.get("keepaStats") or {}).get("lastCheckedDateTime")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def derive(drop, vat_registered=True, inbound_per_unit=0.0):
    """Pull the numbers the filter reasons about into one flat dict.

    Profit figures come straight from ArbiSource (it already knows the fee model).
    The cost basis is backed out of profit/ROI rather than guessed, so it stays
    correct whichever VAT treatment the account is on.
    """
    suffix = "VAT" if vat_registered else ""
    profit_now = _num(drop.get(f"profit{suffix}"), _num(drop.get("profit")))
    roi_now = _num(drop.get(f"ROI{suffix}"), _num(drop.get("ROI")))
    profit_90 = _num(drop.get(f"profit{suffix}90"), _num(drop.get("profit90")))
    profit_30 = _num(drop.get(f"profit{suffix}30"), _num(drop.get("profit30")))

    # ArbiSource's own `inboundShippingCost` is charged PER KILOGRAM, not per unit —
    # setting it to 1.00 took £3.16 off a 3.2kg laptop but only £0.20 off a 200g bottle.
    # A flat per-unit cost therefore has to be applied here instead, with that field
    # left at 0 so nothing is counted twice.
    if inbound_per_unit:
        profit_now -= inbound_per_unit
        profit_90 -= inbound_per_unit
        profit_30 -= inbound_per_unit

    # ArbiSource's own ROI field divides profit by `storePrice` — the PRE-discount
    # list price — while the profit figures are calculated from the discounted price
    # actually paid. On a 72%-off item that understates ROI by ~4x. So ROI is
    # recomputed here against what the stock really costs.
    #
    # Verified against a live drop: sell 24.74 / 1.2 - fees 6.4362
    #   - storeDiscountedPriceExclVAT 5.825 - fixedCost 0.6 = 7.7555 == profitVAT.
    cash_price = _num(drop.get("storeDiscountedPrice")) or _num(drop.get("storePrice"))
    list_price = _num(drop.get("storePrice")) or cash_price
    if vat_registered:
        # VAT on the purchase is reclaimable, so the economic cost is the net figure.
        cost = (_num(drop.get("storeDiscountedPriceExclVAT"))
                or _num(drop.get("storePriceExclVAT"))
                or cash_price / 1.2)
    else:
        cost = cash_price
    if cost <= 0:
        cost = cash_price

    ks = drop.get("keepaStats") or {}
    avg = drop.get("avgSellingPrices") or {}
    avg30 = _num(avg.get("30"), _num((ks.get("avg30") or {}).get("sellingPrice")))
    avg90 = _num(avg.get("90"), _num((ks.get("avg90") or {}).get("sellingPrice")))

    current_sell = _num(drop.get("buyBoxPrice"))
    if not current_sell:
        current_sell = _num(drop.get("lowestFBA")) or _num(drop.get("lowestFBM"))

    variations = max(int(drop.get("variationCount") or 1), 1)
    drops30 = int((ks.get("salesRankDrops30") or 0))

    return {
        "cost": cost,                    # economic cost basis used for every ROI here
        "cash_price": cash_price,        # what actually leaves the bank account
        "list_price": list_price,        # pre-discount, for showing the saving
        "discounted": cash_price < list_price * 0.99,
        "roi_reported": roi_now,         # ArbiSource's own figure, kept for comparison
        "profit_now": profit_now,
        "roi_now": (profit_now / cost * 100.0) if cost else 0.0,
        "profit_90": profit_90,
        "profit_30": profit_30,
        "roi_90": (profit_90 / cost * 100.0) if cost else 0.0,
        "roi_30": (profit_30 / cost * 100.0) if cost else 0.0,
        "current_sell": current_sell,
        "avg30": avg30,
        "avg90": avg90,
        "spike_ratio": (current_sell / avg90) if avg90 else None,
        "trend_ratio": (avg30 / avg90) if avg90 else None,
        "bsr": int(drop.get("BSR") or 0),
        "bsr_pct": _num(drop.get("BSRPercentage")),
        "rank_drops_30": drops30,
        "rank_drops_per_variation": drops30 / variations,
        "monthly_sold": int(ks.get("monthlySold") or 0),
        "variations": variations,
        "sellers": int(drop.get("numberOfSellers") or 0),
        "fba_offers": int(ks.get("offerCountFBA") or 0),
        "keepa_age_days": _keepa_age_days(drop),
        "in_stock": str(drop.get("stockStatus", "")).lower() == "in stock",
        "ip_status": str(drop.get("IPStatus", "") or "unknown").lower(),
        "reduction": _num(drop.get("reduction")),
        "ean_match": bool(drop.get("EANMatch")),
        "store_ean": drop.get("storeEAN"),
        "confidence": _num(drop.get("confidence")),
        "similarity": max(_num(drop.get("stringSimilarityScore")),
                          _num(drop.get("imageSimilarityScore"))),
        "model_codes": sorted(_model_codes(drop.get("storeProductName"))
                              & _model_codes(drop.get("amazonProductName"))),
        "amazon_name": drop.get("amazonProductName"),
        "hazmat": bool(drop.get("hazmat")),
    }


# Words that turn an excluded keyword into a harmless description of an accessory or
# consumable — "dishwasher safe" on an air fryer, "freezer proof" on a food container.
_QUALIFIERS = r"(?:safe|proof|friendly|compatible|tablets?|salt|rinse|cleaner|detergent|bags?|liner|gel|pods?)"


def _excluded_reason(drop, cfg):
    """Why this drop is an unwanted product type, or None if it is fine."""
    name = " ".join(str(drop.get(k) or "") for k in
                    ("storeProductName", "amazonProductName")).lower()

    # A category hit can be overridden — Amazon files PC peripherals under
    # "PC & Video Games", so a mechanical keyboard is not a console game.
    cat = str(drop.get("amazonCategory") or "").lower()
    for bad in cfg["excluded_categories"]:
        if bad.lower() in cat:
            for ok in cfg["category_allow_keywords"]:
                if re.search(rf"\b{re.escape(ok)}\b", name):
                    return None
            return f"excluded category ({drop.get('amazonCategory')})"

    haystack = f"{name} {str(drop.get('storeCategory') or '').lower()}"
    for kw in cfg["excluded_keywords"]:
        if re.search(rf"\b{re.escape(kw)}\b(?!\s+{_QUALIFIERS})", haystack):
            return f"excluded product type ('{kw}')"
    return None


def _velocity(m, cfg):
    """Rank drops in 30 days, per variation unless configured otherwise."""
    if cfg.get("divide_drops_by_variations", True):
        return m["rank_drops_per_variation"]
    return m["rank_drops_30"]


def gate(drop, m, cfg):
    """Return a list of reasons the drop should NOT be notified. Empty list == passes."""
    r = []

    unwanted = _excluded_reason(drop, cfg)
    if unwanted:
        r.append(unwanted)

    if cfg["require_in_stock"] and str(drop.get("stockStatus", "")).lower() != "in stock":
        r.append(f"out of stock ({drop.get('stockStatus')})")
    if cfg["require_green_ip"] and str(drop.get("IPStatus", "")).lower() != "green":
        r.append(f"IP status {drop.get('IPStatus')}")
    if cfg["exclude_hazmat"] and drop.get("hazmat"):
        r.append("hazmat")
    if cfg["exclude_oversized"] and drop.get("oversized"):
        r.append("oversized")
    # Amazon competing directly is a margin killer — but if it shares the buy box
    # rather than holding it outright, the listing is still workable.
    if (cfg["exclude_amazon_on_listing"] and drop.get("amazonOnListing")
            and not drop.get("amazonShareBuyBox")):
        r.append("Amazon holds the listing")

    # --- is this even the same product? ---
    # A missing EAN does not mean a mismatch, so it only raises the confidence bar.
    # But an EAN match is not proof either: barcodes get recycled and retailer feeds
    # carry wrong ones. A Costco "Ninja PC201UK Pressure Cooker" came back EAN-matched
    # to a "Ninja Artisan Pizza Oven MO201UK" with 0.15 title and 0.00 image similarity.
    ean = bool(drop.get("EANMatch"))
    conf = _num(drop.get("confidence"))
    sim = max(_num(drop.get("stringSimilarityScore")), _num(drop.get("imageSimilarityScore")))

    codes_store = _model_codes(drop.get("storeProductName"))
    codes_amazon = _model_codes(drop.get("amazonProductName"))
    codes_agree = bool(codes_store & codes_amazon)
    codes_conflict = bool(codes_store and codes_amazon and not codes_agree)

    if codes_conflict:
        r.append(f"model code mismatch ({'/'.join(sorted(codes_store))} vs "
                 f"{'/'.join(sorted(codes_amazon))})")
    elif not ean and conf < cfg["min_confidence"] and not codes_agree:
        # A shared model code is stronger evidence than any similarity score, so it
        # rescues genuine matches the confidence floor would otherwise bin.
        r.append(f"weak match (no EAN, confidence {conf:.2f})")
    elif ean and sim < cfg["ean_min_corroboration"] and not codes_agree:
        r.append(f"EAN match contradicted by title/image (similarity {sim:.2f})")

    if m["cost"] <= 0:
        r.append("no usable cost")
    elif m["cash_price"] > cfg["max_cost"]:
        # Capped on cash out the door, not the VAT-net figure — that is what limits
        # how much stock can actually be bought.
        r.append(f"costs £{m['cash_price']:.2f} over cap")

    # The core test: does it still work at the price this listing actually trades at?
    if m["roi_90"] < cfg["min_roi"]:
        r.append(f"ROI@90d {m['roi_90']:.1f}% < {cfg['min_roi']}%")
    if m["profit_90"] < cfg["min_profit"]:
        r.append(f"profit@90d £{m['profit_90']:.2f} < £{cfg['min_profit']}")
    if m["profit_now"] < cfg["min_profit_spot"]:
        r.append(f"profit today £{m['profit_now']:.2f} too thin")

    # Sales evidence. Rank *percentile* is only a proxy for "does it sell" — in a huge
    # category like DIY & Tools a 1-3% rank can still be 18+ sales a month. So the
    # percentile only disqualifies when the actual movement is weak too; measured
    # velocity is the stronger signal and it wins.
    strong_velocity = _velocity(m, cfg) >= cfg["min_rank_drops_30"]
    if m["bsr"] <= 0:
        r.append("no BSR (no sales history)")
    elif m["bsr_pct"] > cfg["max_bsr_percent"] and not strong_velocity:
        r.append(f"BSR {m['bsr_pct']:.2f}% of category with weak movement")
    elif m["bsr_pct"] > cfg["hard_max_bsr_percent"]:
        r.append(f"BSR {m['bsr_pct']:.2f}% of category")
    if not strong_velocity:
        if cfg.get("divide_drops_by_variations", True):
            r.append(f"only {m['rank_drops_30']} rank drops/30d over {m['variations']} variation(s)")
        else:
            r.append(f"only {m['rank_drops_30']} rank drops/30d")

    if m["keepa_age_days"] is None:
        r.append("no Keepa timestamp")
    elif m["keepa_age_days"] > cfg["max_keepa_age_days"]:
        r.append(f"Keepa data {m['keepa_age_days']}d stale")

    # Competition.
    if m["sellers"] > cfg["max_sellers"]:
        r.append(f"{m['sellers']} sellers")

    # Price realism.
    if m["spike_ratio"] is not None and m["spike_ratio"] > cfg["spike_tolerance"]:
        r.append(f"current price {(m['spike_ratio'] - 1) * 100:.0f}% above 90d avg (spike)")
    if m["trend_ratio"] is not None and m["trend_ratio"] < cfg["decline_tolerance"]:
        r.append(f"price declining ({(1 - m['trend_ratio']) * 100:.0f}% below 90d avg)")

    return r


def score(m, cfg):
    """0-100 quality score. Everything scored here has already cleared the gates,
    so this is about priority, not pass/fail. Bands are set against what actually
    turns up in the feed, not against a theoretical ideal."""
    # Clearing every gate is itself information — these are already deals worth a look,
    # so the scale starts above zero rather than making good deals look like failures.
    base_s = 10.0

    # ROI at the 90-day average: the gate floor -> 0, floor+60 -> full marks.
    # Weighted highest because cheap stock scales — a £6 buy returning 190% is bought
    # fifty at a time, which a raw profit figure never reflects.
    roi_s = max(0.0, min(1.0, (m["roi_90"] - cfg["min_roi"]) / 60.0)) * 35

    # Absolute profit: the gate floor -> 0, ~£30 -> full marks. Deliberately a smaller
    # weight than ROI so low-cost, high-turnover stock is not penalised.
    prof_s = max(0.0, min(1.0, (m["profit_90"] - cfg["min_profit"]) / 26.0)) * 15

    # Velocity: the gate floor -> 0, ~a sale a day -> full marks.
    floor = cfg["min_rank_drops_30"]
    vel_s = max(0.0, min(1.0, (_velocity(m, cfg) - floor) / 35.0)) * 25

    # Price stability: reward the current price sitting at or below the 90d average.
    if m["spike_ratio"] is None:
        stab_s = 7.5
    else:
        # at the spike limit -> 0, 35% below it -> full marks.
        stab_s = max(0.0, min(1.0, (cfg["spike_tolerance"] - m["spike_ratio"]) / 0.35)) * 15

    total = base_s + roi_s + prof_s + vel_s + stab_s

    # Competition penalty — a crowded listing erodes the margin you modelled.
    if m["sellers"] > 15:
        total -= min(10.0, (m["sellers"] - 15) * 0.7)

    # These are allowed through rather than gated out, so they are priced in here:
    # a deal you cannot buy today, or one carrying IP risk, ranks below a clean one.
    if not m["in_stock"]:
        total -= cfg["penalty_out_of_stock"]
    if m["ip_status"] not in ("green", "unknown"):
        total -= cfg["penalty_ip_flag"]
    if m["hazmat"]:
        total -= cfg["penalty_hazmat"]

    return max(0.0, min(100.0, total))


def tier(s, cfg):
    """Priority band. The gates decide whether a drop is worth seeing at all;
    this only decides how loudly to announce it."""
    return "elite" if s >= cfg["tier_elite"] else "strong"


def evaluate(drop, cfg=None, vat_registered=True):
    """Full assessment of one drop."""
    c = dict(DEFAULTS)
    if cfg:
        c.update({k: v for k, v in cfg.items() if v is not None})
    m = derive(drop, vat_registered=vat_registered,
               inbound_per_unit=c.get("inbound_per_unit", 0.0))
    reasons = gate(drop, m, c)
    s = score(m, c) if not reasons else 0.0
    t = tier(s, c) if not reasons else "reject"

    # A collapse this steep is more often a mis-scrape than a deal — the Stoves cooker
    # that read £1,149 -> £149 in two hours was still £1,149 on the site. Such drops
    # are still shown, but they must not fire an @here.
    suspect = m["reduction"] >= c["suspect_discount"]
    if suspect and c["suspect_discount_blocks_ping"] and t == "elite":
        t = "strong"

    return {
        "pass": not reasons,
        "score": s,
        "tier": t,
        "suspect_price": suspect,
        "reasons": reasons,
        "metrics": m,
    }

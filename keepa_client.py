import logging
import re
from typing import Optional

import keepa

logger = logging.getLogger(__name__)

# Keepa domain string for amazon.co.uk
DOMAIN_UK = 'GB'

# Indices into the Keepa csv[] price history arrays
IDX_AMAZON = 0    # sold directly by Amazon
IDX_NEW = 1       # lowest New offer, any fulfilment (includes Amazon's own)
IDX_FBA = 7       # 3rd-party FBA (New) — stride-3 array
IDX_FBM = 10      # New, FBA (Keepa calls this NEW_FBA)
IDX_COUNT_NEW = 11  # number of New offers
IDX_BUY_BOX = 18  # Buy Box price including shipping


class KeepaClient:
    def __init__(self, config):
        self.api_key = config['keepa_api_key']
        self.domain = config['keepa'].get('domain', DOMAIN_UK)
        self.max_lookups = config['keepa']['max_lookups_per_run']
        self.api = keepa.Keepa(self.api_key, timeout=60.0)

    def lookup_products_by_ean(self, eans: list[str]) -> list[dict]:
        """
        Batch-query Keepa by EAN list.
        Caps at max_lookups_per_run and deduplicates.
        Returns a list of parsed product dicts.
        """
        results = []
        for batch_results in self.lookup_products_by_ean_batched(eans):
            results.extend(batch_results)
        return results

    def lookup_products_by_ean_batched(self, eans: list[str]):
        """
        Generator that yields one batch of parsed product dicts at a time.
        Allows the caller to process and send deals immediately after each batch
        rather than waiting for all batches to complete.
        """
        eans = list(dict.fromkeys(eans))[:self.max_lookups]  # dedupe, preserve order, cap
        if not eans:
            return

        batch_size = 100  # Keepa allows up to 100 products per request
        total_batches = (len(eans) + batch_size - 1) // batch_size

        for i in range(0, len(eans), batch_size):
            batch = eans[i:i + batch_size]
            batch_num = i // batch_size + 1
            logger.info(f"[keepa] Querying {len(batch)} EANs (batch {batch_num}/{total_batches})")
            batch_set = set(batch)
            batch_results = []
            try:
                products = self.api.query(
                    batch,
                    product_code_is_asin=False,
                    extra_params={'idType': 'EAN'},
                    domain=self.domain,
                    history=True,
                    to_datetime=False,  # keep raw integer timestamps so price parsing is reliable
                )
                for product in (products or []):
                    if product is None:
                        continue
                    parsed = self._parse_product(product)
                    if not parsed:
                        continue
                    # Keepa's eanList[0] may differ from the EAN we queried.
                    # Walk all EANs on the returned product to find the queried one.
                    all_eans = [str(e) for e in (product.get('eanList') or [])]
                    queried_ean = next((e for e in all_eans if e in batch_set), None)
                    if queried_ean:
                        parsed['ean'] = queried_ean
                    batch_results.append(parsed)
            except Exception as e:
                logger.error(f"[keepa] Batch query failed: {e}")
            yield batch_results

    def lookup_products_by_asin_batched(self, asins: list[str]):
        """
        Same as lookup_products_by_ean_batched but for known ASINs.

        Callers that have already resolved a product to an ASIN (and cached it)
        can re-price it for 1 token, instead of paying ~16 for another title
        search. Parsed dicts carry 'asin', so the caller maps results back itself.
        """
        asins = list(dict.fromkeys(asins))[:self.max_lookups]
        if not asins:
            return

        batch_size = 100
        total_batches = (len(asins) + batch_size - 1) // batch_size

        for i in range(0, len(asins), batch_size):
            batch = asins[i:i + batch_size]
            batch_num = i // batch_size + 1
            logger.info(f"[keepa] Querying {len(batch)} ASINs (batch {batch_num}/{total_batches})")
            batch_results = []
            try:
                products = self.api.query(
                    batch,
                    product_code_is_asin=True,
                    domain=self.domain,
                    history=True,
                    to_datetime=False,
                )
                for product in (products or []):
                    if product is None:
                        continue
                    parsed = self._parse_product(product)
                    if parsed:
                        batch_results.append(parsed)
            except Exception as e:
                logger.error(f"[keepa] ASIN batch query failed: {e}")
            yield batch_results

    def lookup_by_title(self, title: str) -> Optional[dict]:
        """Search Amazon UK by title keywords; return best-matching product or None."""
        keywords = _search_keywords(title)
        if not keywords:
            return None

        logger.info(f"[keepa] Title search: {keywords!r}")
        try:
            asins = self.api.product_finder(
                {'title': keywords},
                domain=self.domain,
                n_products=5,
            )
        except Exception as e:
            logger.debug(f"[keepa] product_finder failed: {e}")
            return None

        if not asins:
            return None

        try:
            products = self.api.query(
                list(asins)[:5],
                product_code_is_asin=True,
                domain=self.domain,
                history=True,
                to_datetime=False,
            )
        except Exception as e:
            logger.debug(f"[keepa] ASIN query for title match failed: {e}")
            return None

        src_specs = _extract_specs(title)
        src_tokens = set(_title_tokens(title))
        best: Optional[dict] = None
        best_score = 0.0
        best_confident = False

        for product in (products or []):
            if not product:
                continue
            parsed = self._parse_product(product)
            if not parsed:
                continue
            cand_specs = _extract_specs(parsed.get('title', ''))
            compatible, confident = _specs_compatible(src_specs, cand_specs)
            if not compatible:
                logger.debug(
                    f"[keepa] Spec mismatch src={src_specs} cand={cand_specs} "
                    f"— {parsed.get('title', '')[:50]}"
                )
                continue
            cand_tokens = set(_title_tokens(parsed.get('title', '')))
            score = (len(src_tokens & cand_tokens) / len(src_tokens | cand_tokens)
                     if src_tokens and cand_tokens else 0.0)
            min_score = 0.0 if src_specs else 0.3
            if score > best_score and score >= min_score:
                best_score = score
                best = parsed
                best_confident = confident

        if best:
            best['match_type'] = 'title'
            best['match_unsure'] = not best_confident
            logger.info(
                f"[keepa] Title match score={best_score:.2f} confident={best_confident} "
                f"specs={src_specs}: {best.get('title', '')[:60]}"
            )
        else:
            logger.debug(f"[keepa] No title match found for: {title[:60]}")

        return best

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_product(self, product: dict) -> Optional[dict]:
        asin = product.get('asin')
        if not asin:
            return None

        csv = product.get('csv') or []

        def get_price(idx: int) -> Optional[float]:
            """Return the most recent price in £ from a csv history array, or None."""
            if idx >= len(csv) or csv[idx] is None:
                return None
            arr = csv[idx]
            # Most csv slots: [timestamp, price, timestamp, price, ...] — stride 2
            # csv[7] (FBA 3P New): [timestamp, price, seller_count, ...] — stride 3
            stride = 3 if idx == IDX_FBA else 2
            prices = [arr[i] for i in range(1, len(arr), stride) if arr[i] and arr[i] > 0]
            return prices[-1] / 100.0 if prices else None

        def get_min_recent_price(idx: int, n: int = 5) -> Optional[float]:
            """Return the minimum of the last n prices — avoids single high-priced seller outliers."""
            if idx >= len(csv) or csv[idx] is None:
                return None
            arr = csv[idx]
            stride = 3 if idx == IDX_FBA else 2
            prices = [arr[i] for i in range(1, len(arr), stride) if arr[i] and arr[i] > 0]
            if not prices:
                return None
            return min(prices[-n:]) / 100.0

        def current_price(idx: int) -> Optional[float]:
            """Most recent price in £, or None if there is no LIVE offer.

            Keepa records price CHANGES, and -1 means "the offer ended here". Filtering
            -1 out and taking the last positive value returns whatever the price was
            before the offer stopped — potentially months stale. That bug made a
            discontinued £159.33 Amazon offer look live when the real price was £74.99,
            and every alert it produced was a false positive.
            """
            if idx >= len(csv) or not csv[idx]:
                return None
            arr = csv[idx]
            if len(arr) < 2:
                return None
            # stride 2: [ts, price, ...]  |  stride 3: [ts, price, seller_count, ...]
            last = arr[-2] if idx == IDX_FBA else arr[-1]
            if last is None or last <= 0:      # -1 / -2 => no current offer
                return None
            return last / 100.0

        # Order matters: take what a buyer actually pays right now. Buy Box first,
        # then the lowest live New offer (which already includes Amazon's own price
        # when Amazon is selling), then Amazon direct, then FBA.
        buy_box = current_price(IDX_BUY_BOX)
        new_price = current_price(IDX_NEW)
        amazon_price = current_price(IDX_AMAZON)
        fba_price = current_price(IDX_FBA)

        if buy_box:
            selling_price, price_type = buy_box, 'BuyBox'
        elif new_price:
            selling_price, price_type = new_price, 'New'
        elif amazon_price:
            selling_price, price_type = amazon_price, 'Amazon'
        elif fba_price:
            selling_price, price_type = fba_price, 'FBA'
        else:
            logger.debug(f"[keepa] No live offer for ASIN {asin} — skipping")
            return None

        # --- spike guard -------------------------------------------------------
        # A live price is not automatically a sellable price. When Amazon and the
        # Buy Box drop out, a single remaining seller can park the listing far above
        # what it ever sells for. Example: SALTER EK6757 showed a live £149.50 from
        # one FBM seller with no Buy Box, against a 90-day average of £98.72 — it
        # looked like a 21.6% ROI lead at Currys' £90.99 and was worthless.
        # Price against the lower of (live, 90-day average) so spikes can't qualify.
        def avg_recent(idx: int, days: int = 90) -> Optional[float]:
            if idx >= len(csv) or not csv[idx]:
                return None
            arr = csv[idx]
            stamps, vals = arr[::2], arr[1::2]
            if not stamps:
                return None
            cutoff = max(stamps) - days * 1440      # Keepa timestamps are minutes
            recent = [v for t, v in zip(stamps, vals)
                      if v and v > 0 and t >= cutoff]
            return (sum(recent) / len(recent) / 100.0) if recent else None

        avg90 = avg_recent(IDX_BUY_BOX) or avg_recent(IDX_NEW) or avg_recent(IDX_AMAZON)
        live_price = selling_price
        if avg90 and selling_price > avg90:
            selling_price = avg90
            price_type += '~avg90'
            logger.debug(
                f"[keepa] {asin}: live £{live_price:.2f} above 90d avg £{avg90:.2f} "
                f"— pricing against the average"
            )

        # Offer count — 1 seller and no Buy Box is the classic spike signature
        offer_count = None
        if IDX_COUNT_NEW < len(csv) and csv[IDX_COUNT_NEW]:
            counts = csv[IDX_COUNT_NEW][1::2]
            if counts and counts[-1] is not None and counts[-1] >= 0:
                offer_count = counts[-1]

        # FBA pick-and-pack fee (Keepa stores in pence)
        fba_fees_raw = product.get('fbaFees') or {}
        pick_pack_raw = fba_fees_raw.get('pickAndPackFee') or 0
        fba_fee = pick_pack_raw / 100.0

        # EAN — take first entry from Keepa's eanList
        ean_list = product.get('eanList') or []
        ean = ean_list[0] if ean_list else None

        images_csv = product.get('imagesCSV') or ''
        first_image = images_csv.split(',')[0].strip() if images_csv else ''
        amazon_img = (
            f"https://images-na.ssl-images-amazon.com/images/I/{first_image}"
            if first_image else ''
        )

        # Review count — csv[16], raw integer count, stride 2
        review_count = None
        if len(csv) > 16 and csv[16]:
            arr = csv[16]
            counts = [arr[i] for i in range(1, len(arr), 2) if arr[i] and arr[i] > 0]
            if counts:
                review_count = int(counts[-1])

        # Rating — csv[17], stored as rating * 10 (e.g. 47 = 4.7 stars), stride 2
        rating = None
        if len(csv) > 17 and csv[17]:
            arr = csv[17]
            ratings = [arr[i] for i in range(1, len(arr), 2) if arr[i] and arr[i] > 0]
            if ratings:
                rating = round(ratings[-1] / 10.0, 1)

        # Monthly sold — use Keepa's direct figure, fall back to BSR drop count
        monthly_sold = product.get('monthlySold') or _estimate_monthly_sales_from_drops(product)

        return {
            'asin': asin,
            'title': product.get('title', ''),
            'selling_price': selling_price,
            'price_type': price_type,
            'live_price': live_price,    # what a single seller is asking right now
            'avg90': avg90,              # 90-day average; selling_price is capped to it
            'offer_count': offer_count,
            'has_buy_box': buy_box is not None,
            'fba_fee': fba_fee,
            'bsr': self._get_bsr(product),
            'review_count': review_count,
            'rating': rating,
            'monthly_sold': monthly_sold,
            # Proof-of-sale over a wider window, so a product with unknown monthly
            # sales can still be shown if its rank demonstrably moves.
            'rank_drops_90d': count_rank_drops(product, days=90),
            'ean': ean,
            'amazon_url': f"https://www.amazon.co.uk/dp/{asin}",
            'amazon_img': amazon_img,
            'match_type': 'ean',
        }

    @staticmethod
    def _get_bsr(product: dict) -> Optional[int]:
        """Extract the most recent BSR from Keepa product data."""
        # salesRanks format: {category_id: [ts, rank, ts, rank, ...]}
        sales_ranks = product.get('salesRanks') or {}
        for rank_history in sales_ranks.values():
            if rank_history and len(rank_history) >= 2:
                # Last value in the array is the most recent rank
                return int(rank_history[-1])

        return None


# A sale moves the rank sharply (e.g. 280k -> 60k). A listing that is NOT selling
# still wobbles by fractions of a percent as neighbouring listings drift, and counting
# those as sales turned a dead TKL keyboard (276k -> 283k) into "11 sales/month".
MIN_SALE_RANK_DROP = 0.05   # rank must improve by at least 5% to count as a sale


def count_rank_drops(product: dict, days: int = 30) -> Optional[int]:
    """Count sharp BSR improvements over `days` — each one implies a sale.

    Uses the listing's main sales rank (salesRankReference, else rootCategory), not
    a subcategory, and ignores sub-5% wobbles that happen without any sale.

    Returns None only when there is no usable sales-rank history at all, which is
    itself meaningful: a listing Keepa has never seen rank on has no evidence of
    ever selling. 0 means history exists but nothing sold in the window.
    """
    import time as _time
    sales_ranks = product.get('salesRanks') or {}
    if not sales_ranks:
        return None
    # Keepa timestamps are minutes since 21 Jan 2011 00:00:00 UTC
    KEEPA_EPOCH = 1295395200
    cutoff = int((_time.time() - KEEPA_EPOCH - days * 24 * 3600) / 60)
    best = None
    for ref in (product.get('salesRankReference'), product.get('rootCategory')):
        if ref not in (None, -1) and str(ref) in {str(k) for k in sales_ranks}:
            best = next(v for k, v in sales_ranks.items() if str(k) == str(ref))
            break
    if best is None:
        best = max(sales_ranks.values(), key=len, default=None)
    if not best or len(best) < 4:
        return None
    drops = 0
    prev = None
    for i in range(0, len(best) - 1, 2):
        if best[i] < cutoff:
            continue
        rank = best[i + 1]
        if rank <= 0:
            continue
        if prev is not None and rank < prev * (1 - MIN_SALE_RANK_DROP):
            drops += 1
        prev = rank
    return drops


def _estimate_monthly_sales_from_drops(product: dict) -> Optional[int]:
    """30-day rank-drop count as a rough monthly sales proxy, or None if zero/absent."""
    drops = count_rank_drops(product, days=30)
    return drops if drops else None


def _search_keywords(title: str) -> str:
    """Return first 7 words of a title, stripped of punctuation, for Keepa product_finder."""
    clean = re.sub(r'[^\w\s]', ' ', title)
    return ' '.join(clean.split()[:7])


def _title_tokens(title: str) -> list[str]:
    """Lowercase alphanumeric tokens for Jaccard similarity comparison."""
    return [t.lower() for t in re.findall(r'[a-z0-9]+', title.lower()) if len(t) > 1]


# Colour groups: canonical name → set of title variants (longest checked first)
_COLOUR_GROUPS: dict = {
    'black':    {'jet black', 'midnight black', 'matte black', 'onyx black', 'obsidian black',
                 'charcoal black', 'onyx', 'obsidian', 'charcoal', 'black'},
    'silver':   {'natural silver', 'cool silver', 'titan silver', 'moonlight silver',
                 'platinum silver', 'platinum', 'silver'},
    'gold':     {'warm gold', 'rose gold', 'sand gold', 'champagne gold', 'starlight',
                 'champagne', 'gold'},
    'blue':     {'midnight blue', 'pacific blue', 'ice blue', 'cobalt blue', 'lunar blue',
                 'navy blue', 'cobalt', 'indigo', 'navy', 'blue'},
    'graphite': {'dark graphite', 'space grey', 'space gray', 'dark grey', 'dark gray',
                 'titanium grey', 'titanium gray', 'graphite', 'titanium', 'slate'},
    'white':    {'pure white', 'cloud white', 'lunar light', 'snow white', 'ivory', 'white'},
    'green':    {'dark green', 'sage green', 'forest green', 'olive green',
                 'sage', 'olive', 'mint', 'green'},
    'pink':     {'rose pink', 'blush pink', 'coral pink', 'blush', 'coral', 'pink'},
    'red':      {'scarlet', 'crimson', 'burgundy', 'red'},
    'purple':   {'dark purple', 'violet', 'lilac', 'lavender', 'purple'},
}


def _extract_colour(title_lower: str) -> Optional[str]:
    """Return canonical colour group name found in title, or None."""
    for group, variants in _COLOUR_GROUPS.items():
        for variant in sorted(variants, key=len, reverse=True):
            if re.search(r'\b' + re.escape(variant) + r'\b', title_lower):
                return group
    return None


def _extract_specs(title: str) -> dict:
    """
    Extract key product specs from a title:
      screen (inches), storage_gb, ram_gb, type, colour
    Only specs actually found in the title are returned.
    """
    t = title.lower()
    specs: dict = {}

    # Screen size: XX.X" / XX.X inch / XX.X (before laptop/notebook — Currys style)
    m = (re.search(r'\b(\d{2}\.?\d*)\s*(?:["\']|-?inch\b)', t) or
         re.search(r'\b(\d{2}\.\d)\b(?!\s*(?:gb|tb|mhz|mm|cm|w\b|g\b))', t))
    if m:
        val = float(m.group(1))
        if 10.0 <= val <= 22.0:
            specs['screen'] = round(val, 1)

    # RAM: must have an explicit RAM/Memory label to avoid confusion with storage
    m = re.search(r'\b(\d+)\s*gb\s+(?:ram|memory|ddr\w*)', t)
    if m:
        specs['ram_gb'] = int(m.group(1))

    # Storage: keyword form first (SSD/HDD/NVMe/eMMC), then fallback to >=64 GB lone value
    m = re.search(r'\b(\d+)\s*(tb|gb)\s*(?:ssd|hdd|nvme|emmc|flash\b|storage\b)', t)
    if m:
        v, u = int(m.group(1)), m.group(2)
        specs['storage_gb'] = v * 1024 if u == 'tb' else v
    else:
        for v_str, u in re.findall(r'\b(\d+)\s*(tb|gb)\b', t):
            v = int(v_str)
            gb = v * 1024 if u == 'tb' else v
            if gb >= 64:
                specs['storage_gb'] = gb
                break

    # Product type
    if 'laptop' in t or 'notebook' in t:
        specs['type'] = 'laptop'

    # Colour
    colour = _extract_colour(t)
    if colour:
        specs['colour'] = colour

    return specs


def _specs_compatible(src: dict, cand: dict) -> tuple[bool, bool]:
    """
    Return (compatible, confident).
    compatible — no spec conflicts found (definite no = False).
    confident  — at least 2 specs were directly compared (definite yes = True).
    Specs absent from either title are skipped; they don't count toward confidence.
    """
    compared = 0
    for key in ('type', 'storage_gb', 'ram_gb', 'colour'):
        sv, cv = src.get(key), cand.get(key)
        if sv is not None and cv is not None:
            compared += 1
            if sv != cv:
                return False, False
    sv, cv = src.get('screen'), cand.get('screen')
    if sv is not None and cv is not None:
        compared += 1
        if abs(sv - cv) > 0.2:
            return False, False
    return True, compared >= 2

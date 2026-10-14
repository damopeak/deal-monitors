"""
A2A Deal Finder — continuously polls Keepa's Deal API for Amazon price drops
and alerts on Discord when the drop is profitable to buy and resell via FBA.

Buy price  = current Amazon price (discounted)
Sell price = 90-day average Amazon price (the normal price)

Keepa deal response field notes:
  current[0]         = current Amazon price (pence)
  current[3]         = current BSR
  avg[0][0]          = 30-day avg Amazon price
  avg[1][0]          = 90-day avg Amazon price  <- sell price
  deltaPercent[1][0] = 90-day % drop for Amazon price
  image              = list of ASCII codepoints forming the image filename
"""

import io
import json
import logging
import time
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import keepa
import matplotlib
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import requests

matplotlib.use('Agg')

from calculator import calculate_roi
from config_loader import load_config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

KEEPA_DEAL_URL = "https://api.keepa.com/deal"
KEEPA_EPOCH = 1295395200      # Unix ts for Keepa epoch (21 Jan 2011 UTC)
POLL_INTERVAL = 300            # seconds between polls
SEEN_FILE = Path(__file__).parent / 'a2a_seen.json'
COLOUR_A2A = 0x9B59B6          # purple

WAREHOUSE_LABELS = {1: "New", 2: "Like New", 3: "Very Good", 4: "Good", 5: "Acceptable"}

SELECTION = {
    "page": 0,
    "domainId": "2",
    "excludeCategories": [],
    "includeCategories": [],
    "priceTypes": [0],
    "deltaRange": [0, 2147483647],
    "deltaPercentRange": [30, 80],
    "salesRankRange": [-1, -1],
    "currentRange": [5800, 2147483647],
    "minRating": -1,
    "isLowest": False,
    "isLowest90": False,
    "isLowestOffer": False,
    "isOutOfStock": False,
    "titleSearch": "",
    "isRangeEnabled": True,
    "isFilterEnabled": False,
    "filterErotic": True,
    "singleVariation": True,
    "hasReviews": False,
    "isPrimeExclusive": False,
    "mustHaveAmazonOffer": False,
    "mustNotHaveAmazonOffer": False,
    "sortType": 1,
    "dateRange": "0",
    "warehouseConditions": [1, 2, 3, 4, 5],
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def _save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))


# ---------------------------------------------------------------------------
# Keepa API calls
# ---------------------------------------------------------------------------

def fetch_deals(api_key: str) -> list[dict]:
    resp = requests.get(
        KEEPA_DEAL_URL,
        params={'key': api_key, 'selection': json.dumps(SELECTION)},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    logger.info(f"[a2a] Tokens left: {data.get('tokensLeft', '?')}")
    return data.get('deals', {}).get('dr', [])


def fetch_fba_fees(api: keepa.Keepa, asins: list[str]) -> dict[str, dict]:
    """Cheap query — no price history, just product metadata for FBA fees."""
    result = {}
    for i in range(0, len(asins), 50):
        batch = asins[i:i + 50]
        try:
            products = api.query(batch, product_code_is_asin=True, domain='GB',
                                 history=False, to_datetime=False)
            for p in (products or []):
                if p and p.get('asin'):
                    result[p['asin']] = p
        except Exception as e:
            logger.error(f"[a2a] FBA fee query failed: {e}")
    return result


def fetch_history(api: keepa.Keepa, asins: list[str]) -> dict[str, dict]:
    """Full history query — used only for qualifying products to build charts."""
    result = {}
    for i in range(0, len(asins), 50):
        batch = asins[i:i + 50]
        try:
            products = api.query(batch, product_code_is_asin=True, domain='GB',
                                 history=True, to_datetime=False)
            for p in (products or []):
                if p and p.get('asin'):
                    result[p['asin']] = p
        except Exception as e:
            logger.error(f"[a2a] History query failed: {e}")
    return result


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _pence(arr: list | None, idx: int = 0) -> float | None:
    if not arr or idx >= len(arr) or arr[idx] <= 0:
        return None
    return arr[idx] / 100.0


def _avg90(raw: dict) -> float | None:
    avg = raw.get('avg')
    if not avg or len(avg) < 2:
        return None
    return _pence(avg[1], 0)


def _drop_pct(raw: dict) -> int:
    dp = raw.get('deltaPercent')
    if not dp or not isinstance(dp, list):
        return 0
    d90 = dp[1] if len(dp) > 1 else dp[0]
    if isinstance(d90, list):
        return d90[0] if d90 else 0
    return int(d90 or 0)


def _bsr(raw: dict) -> int | None:
    current = raw.get('current') or []
    val = current[3] if len(current) > 3 else -1
    return val if val > 0 else None


def _thumbnail_url(raw: dict) -> str | None:
    img_bytes = raw.get('image') or []
    if not img_bytes:
        return None
    filename = ''.join(chr(c) for c in img_bytes)
    return f"https://images-na.ssl-images-amazon.com/images/I/{filename}"


# ---------------------------------------------------------------------------
# Referral fee auto-detection (Amazon UK, 2024 schedule)
# ---------------------------------------------------------------------------

# Each entry: (tuple of lowercase substrings to match in the category path,
#              flat_pct or None, tiers list or None)
# Checked in order — first match wins.
_UK_REFERRAL_SCHEDULE = [
    # 7% — Computing & Mobile
    (('computers & accessories', 'computer & office', 'computing',
      'laptops', 'tablets', 'software', 'data storage', 'networking',
      'printers', 'monitors', 'pc components', 'pc peripherals',
      'mobile phones & communication', 'smartphones, tablets & e-readers',
      'mobile phones, smart watches & accessories'),
     7, None),
    # Tiered 15% ≤£100 / 7% above — Consumer Electronics
    (('electronics', 'camera & photo', 'cameras',
      'headphones', 'hi-fi & home audio', 'home audio',
      'tv & home cinema', 'televisions', 'home cinema',
      'gps & navigation', 'car electronics'),
     None, [{'up_to': 100, 'rate': 15}, {'rate': 7}]),
    # 12% — Musical Instruments & Industrial
    (('musical instruments', 'industrial & scientific',
      'business, industry & science'),
     12, None),
    # 20% — Jewellery & Watches
    (('jewellery', 'jewelry', 'watches', 'fine art'),
     20, None),
    # 45% — Amazon Device Accessories
    (('amazon device accessories',),
     45, None),
]
_DEFAULT_REFERRAL_PCT = 15.0


def _referral_rate(product_data: dict) -> tuple[float | None, list | None]:
    """
    Return (flat_pct, tiers) from Amazon UK fee schedule using Keepa category tree.
    Both None → caller falls back to global config default (15%).
    """
    cat_tree = product_data.get('categoryTree') or []
    names = []
    for item in cat_tree:
        if isinstance(item, dict):
            names.append(item.get('name', '').lower())
        elif isinstance(item, str):
            names.append(item.lower())
    cat_str = ' | '.join(names)
    if not cat_str:
        return None, None
    for keywords, flat, tiers in _UK_REFERRAL_SCHEDULE:
        if any(kw in cat_str for kw in keywords):
            logger.debug(f"[referral] {'tiered' if tiers else f'{flat}%'} matched via '{cat_str[:70]}'")
            return flat, tiers
    return None, None  # default 15%


# ---------------------------------------------------------------------------
# Chart generation
# ---------------------------------------------------------------------------

def _keepa_ts_to_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(KEEPA_EPOCH + ts * 60, tz=timezone.utc)


def _fmt_date(x, _):
    dt = mdates.num2date(x)
    return f"{dt.strftime('%b')} {dt.day}"


def generate_price_chart(product: dict) -> bytes | None:
    csv_data = product.get('csv') or []
    sales_ranks_raw = product.get('salesRanks') or {}
    cutoff = int((_time.time() - KEEPA_EPOCH) / 60) - 90 * 24 * 60

    def parse_price(idx: int, stride: int = 2):
        if idx >= len(csv_data) or not csv_data[idx]:
            return [], []
        arr = csv_data[idx]
        ts_list, px_list = [], []
        for i in range(0, len(arr) - stride + 1, stride):
            ts, px = arr[i], arr[i + 1]
            if ts < cutoff or px <= 0:
                continue
            ts_list.append(_keepa_ts_to_dt(ts))
            px_list.append(px / 100.0)
        return ts_list, px_list

    def parse_bsr():
        if not sales_ranks_raw:
            return [], []
        arr = max(sales_ranks_raw.values(), key=len)
        ts_list, rank_list = [], []
        for i in range(0, len(arr) - 1, 2):
            ts, rank = arr[i], arr[i + 1]
            if ts < cutoff or rank <= 0:
                continue
            ts_list.append(_keepa_ts_to_dt(ts))
            rank_list.append(rank)
        return ts_list, rank_list

    amz_t, amz_p = parse_price(0)   # Amazon   — peach fill
    bb_t,  bb_p  = parse_price(18)  # Buy Box  — magenta line
    bsr_t, bsr_r = parse_bsr()      # BSR      — green line, right axis

    if not amz_t:
        return None

    now_dt = datetime.now(tz=timezone.utc)
    x_min = min(amz_t)

    fig, ax1 = plt.subplots(figsize=(11, 4))
    ax2 = ax1.twinx()
    fig.patch.set_facecolor('#FFFFFF')
    ax1.set_facecolor('#FFFFFF')
    ax2.patch.set_visible(False)
    ax1.set_zorder(1)
    ax2.set_zorder(2)

    # Amazon — peach fill area (no separate price line)
    ax1.fill_between(amz_t, amz_p, 0, step='post', color='#FFDAAA', zorder=1)

    # Horizontal gray gridlines only (matches reference)
    ax1.grid(True, axis='y', color='#CCCCCC', linewidth=0.7, zorder=2)
    ax1.grid(False, axis='x')

    # Buy Box — bold magenta step line
    if bb_t:
        ax1.step(bb_t, bb_p, where='post', color='#FF1493', linewidth=2.0, zorder=4)

    # BSR — thin green step line on right axis (in front of everything)
    curr_bsr = None
    if bsr_t:
        curr_bsr = bsr_r[-1]
        ax2.step(bsr_t, bsr_r, where='post', color='#3CB371', linewidth=1.2, zorder=6)
        ax2.set_ylim(0, max(bsr_r) * 1.15)
        ax2.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda x, _: f'#{int(x):,}')
        )
        ax2.tick_params(axis='y', labelcolor='#555555', labelsize=7.5, length=3)
    else:
        ax2.set_visible(False)

    # Pin x-axis flush against both y-axes
    ax1.set_xlim(x_min, now_dt)
    ax1.set_ylim(bottom=0)

    # Axes formatting
    ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'£{x:.0f}'))
    ax1.tick_params(axis='y', labelsize=8, labelcolor='#333333', length=3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%b %d'))
    ax1.xaxis.set_major_locator(mdates.DayLocator(interval=15))
    plt.setp(ax1.xaxis.get_majorticklabels(), fontsize=8, color='#333333')
    for spine in ax1.spines.values():
        spine.set_color('#BBBBBB')

    # Legend — dot for BSR and Buy Box; square patch for Amazon fill
    legend_handles = []
    if curr_bsr:
        legend_handles.append(plt.Line2D(
            [0], [0], marker='o', color='w', markerfacecolor='#3CB371',
            markersize=9, label=f'BSR: #{curr_bsr:,}'))
    if bb_t:
        legend_handles.append(plt.Line2D(
            [0], [0], marker='o', color='w', markerfacecolor='#FF1493',
            markersize=9, label=f'Buy Box: £{bb_p[-1]:.2f}'))
    if amz_t:
        legend_handles.append(mpatches.Patch(
            facecolor='#FFDAAA', edgecolor='#BBBBBB',
            label=f'Amz: £{amz_p[-1]:.2f}'))

    ax1.legend(handles=legend_handles,
               loc='lower center', bbox_to_anchor=(0.5, -0.22),
               ncol=len(legend_handles), fontsize=9, frameon=False,
               handletextpad=0.4, columnspacing=2.0)

    plt.tight_layout()
    plt.subplots_adjust(bottom=0.22)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=130, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

def _send_deal(webhook_url: str, raw: dict, fba_fee: float, deal: dict,
               chart: bytes | None = None):
    asin = raw['asin']
    title = (raw.get('title') or asin)[:256]

    buy_price = _pence(raw.get('current'), 0)
    avg90_price = _avg90(raw)
    drop = _drop_pct(raw)
    bsr = _bsr(raw)
    monthly_sold = raw.get('monthlySold')
    condition = WAREHOUSE_LABELS.get(raw.get('warehouseCondition', 1), 'New')

    fields = [
        {"name": "Condition",              "value": condition,                                   "inline": True},
        {"name": "Buy Price (Amazon now)", "value": f"£{buy_price:.2f}",                        "inline": True},
        {"name": "Avg 90-Day Price",       "value": f"£{avg90_price:.2f}",                      "inline": True},
        {"name": "Price Drop",             "value": f"{drop}%",                                  "inline": True},
        {"name": "ROI",                    "value": f"{deal['roi']:.1f}%",                       "inline": True},
        {"name": "Profit",                 "value": f"£{deal['profit']:.2f}",                    "inline": True},
        {"name": "FBA Fee",                "value": f"£{fba_fee:.2f}",                           "inline": True},
        {"name": "Referral Fee",           "value": f"£{deal['referral_fee']:.2f}",              "inline": True},
        {"name": "BSR",                    "value": f"{bsr:,}" if bsr else "N/A",                "inline": True},
        {"name": "Monthly Sales",          "value": str(monthly_sold) if monthly_sold else "N/A","inline": True},
        {"name": "Amazon URL",             "value": f"https://www.amazon.co.uk/dp/{asin}",       "inline": False},
    ]

    embed = {
        "title": title,
        "url": f"https://www.amazon.co.uk/dp/{asin}",
        "color": COLOUR_A2A,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    thumb = _thumbnail_url(raw)
    if thumb:
        embed["thumbnail"] = {"url": thumb}

    if chart:
        embed["image"] = {"url": "attachment://chart.png"}

    try:
        if chart:
            resp = requests.post(
                webhook_url,
                data={'payload_json': json.dumps({"embeds": [embed]})},
                files={'file': ('chart.png', chart, 'image/png')},
                timeout=15,
            )
        else:
            resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        resp.raise_for_status()
        logger.info(f"[a2a] Sent: {asin} | {condition} | ROI={deal['roi']:.1f}% | Profit=£{deal['profit']:.2f}")
    except requests.RequestException as e:
        logger.error(f"[a2a] Discord webhook failed: {e}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _process_batch(raw_deals: list[dict], fba_products: dict, config: dict,
                   webhook_url: str, keepa_api: keepa.Keepa) -> int:
    qualifying = []

    for raw in raw_deals:
        asin = raw.get('asin')
        if not asin:
            continue

        buy_price = _pence(raw.get('current'), 0)
        sell_price = _avg90(raw)

        if not buy_price or not sell_price or sell_price <= buy_price:
            continue

        product = fba_products.get(asin, {})
        fba_fee = (product.get('fbaFees') or {}).get('pickAndPackFee', 0) / 100.0

        flat_pct, tiers = _referral_rate(product)
        product_meta: dict = {}
        if tiers is not None:
            product_meta['referral_fee_tiers'] = tiers
        elif flat_pct is not None:
            product_meta['referral_fee_percent'] = flat_pct

        deal = calculate_roi(
            buy_price,
            {'selling_price': sell_price, 'fba_fee': fba_fee, 'price_type': 'avg90'},
            config,
            product=product_meta or None,
        )
        if deal:
            qualifying.append({'raw': raw, 'fba_fee': fba_fee, 'deal': deal, 'asin': asin})

    if not qualifying:
        return 0

    # Fetch full price history only for qualifying products
    logger.info(f"[a2a] {len(qualifying)} qualifying — fetching history for charts...")
    history = fetch_history(keepa_api, [q['asin'] for q in qualifying])

    for q in qualifying:
        product = history.get(q['asin'], {})
        chart = generate_price_chart(product)
        _send_deal(webhook_url, q['raw'], q['fba_fee'], q['deal'], chart=chart)

    return len(qualifying)


def main():
    config = load_config()
    api_key = config['keepa_api_key']
    webhook_url = config['discord_webhook_url']
    keepa_api = keepa.Keepa(api_key, timeout=60.0)

    seen = _load_seen()
    logger.info(f"[a2a] Started — {len(seen)} ASINs already seen. Polling every {POLL_INTERVAL}s.")

    while True:
        try:
            all_deals = fetch_deals(api_key)
            new_deals = [d for d in all_deals if d.get('asin') and d['asin'] not in seen]
            logger.info(f"[a2a] {len(all_deals)} deals total, {len(new_deals)} new")

            if new_deals:
                seen.update(d['asin'] for d in new_deals)
                _save_seen(seen)

                fba_products = fetch_fba_fees(keepa_api, [d['asin'] for d in new_deals])
                sent = _process_batch(new_deals, fba_products, config, webhook_url, keepa_api)
                logger.info(f"[a2a] {sent} qualifying deals sent")

        except requests.HTTPError as e:
            logger.error(f"[a2a] Keepa API error: {e}")
        except Exception as e:
            logger.error(f"[a2a] Unexpected error: {e}", exc_info=True)

        logger.info(f"[a2a] Sleeping {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()

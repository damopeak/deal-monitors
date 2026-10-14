import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

COLOUR_DEAL = 0x00AA00   # green  (EAN match)
COLOUR_TITLE = 0xFFA500  # orange (confident title match)
COLOUR_UNSURE = 0xFF4444  # red    (title match — low confidence, verify manually)
COLOUR_INFO = 0x0055FF   # blue
IMAGE_MATCH_THRESHOLD = 0.75


def _price_basis(amazon_data: dict) -> str:
    """Show whether the sell price is the live one or capped to the 90-day average.

    'live £149.50 → £98.72' means one seller has spiked the listing and we priced
    against what it actually sells for instead.
    """
    live = amazon_data.get('live_price')
    avg90 = amazon_data.get('avg90')
    used = amazon_data.get('selling_price')
    if live and used and live > used + 0.01:
        return f"live £{live:.2f} → £{used:.2f} (90d avg)"
    if avg90:
        return f"live · 90d avg £{avg90:.2f}"
    return "live"


def _match_label(amazon_data: dict) -> str:
    if amazon_data.get('match_type') != 'title':
        return "✅ EAN"
    unsure = amazon_data.get('match_unsure', False)
    img_score = amazon_data.get('img_score')
    if img_score is not None:
        pct = int(img_score * 100)
        icon = "🖼️✅" if img_score >= IMAGE_MATCH_THRESHOLD else "🖼️⚠️"
        label = f"Title + {icon} {pct}%"
    else:
        label = "⚠️ Title"
    if unsure:
        label += " — verify manually"
    return label


class DiscordNotifier:
    def __init__(self, config, webhook_url: str | None = None,
                 username: str | None = None):
        # webhook_url/username let a single service post to its own channel under
        # its own name; omitted, behaviour is exactly as before.
        self.webhook_url = webhook_url or config['discord_webhook_url']
        self.username = username

    def send_deal(self, product: dict, amazon_data: dict, deal: dict):
        """Send one qualifying deal as a rich embed."""
        is_title_match = amazon_data.get('match_type') == 'title'
        is_unsure = amazon_data.get('match_unsure', False)
        if not is_title_match:
            colour = COLOUR_DEAL
        elif is_unsure:
            colour = COLOUR_UNSURE
        else:
            colour = COLOUR_TITLE
        embed = {
            "title": product['title'][:256],
            "color": colour,
            "fields": [
                {"name": "Supplier Price",
                 "value": (
                     f"£{deal['supplier_price']:.2f}"
                     + (f" (£{deal['discount_applied']:.0f} off)" if deal.get('discount_applied') else "")
                 ), "inline": True},
                {"name": f"Amazon Price ({deal['price_type']})",
                 "value": f"£{deal['selling_price']:.2f}", "inline": True},
                {"name": "ROI",
                 "value": f"{deal['roi']:.1f}%", "inline": True},
                {"name": "Profit",
                 "value": f"£{deal['profit']:.2f}", "inline": True},
                {"name": "FBA Fee",
                 "value": f"£{deal['fba_fee']:.2f}", "inline": True},
                {"name": "Referral Fee",
                 "value": f"£{deal['referral_fee']:.2f}", "inline": True},
                # Listing health — shown, never used to auto-reject. A spiked price from
                # a lone seller with no Buy Box is the classic trap; these two fields are
                # what let you spot it without opening Keepa.
                {"name": "Price Basis",
                 "value": _price_basis(amazon_data), "inline": True},
                {"name": "Offers / Buy Box",
                 "value": (
                     f"{amazon_data['offer_count']} offer"
                     + ("s" if amazon_data.get('offer_count') != 1 else "")
                     if amazon_data.get('offer_count') is not None else "? offers"
                 ) + (" · ✅ BB" if amazon_data.get('has_buy_box') else " · ⚠️ no BB"),
                 "inline": True},
                {"name": "BSR",
                 "value": f"{amazon_data['bsr']:,}" if amazon_data.get('bsr') else 'N/A', "inline": True},
                {"name": "Monthly Sales",
                 "value": str(amazon_data['monthly_sold']) if amazon_data.get('monthly_sold') is not None else 'N/A', "inline": True},
                {"name": "EAN",
                 "value": str(product.get('ean') or 'N/A'), "inline": True},
                {"name": "Match",
                 "value": _match_label(amazon_data), "inline": True},
                {"name": "Supplier URL",
                 "value": product['url'], "inline": False},
                {"name": "Amazon URL",
                 "value": amazon_data['amazon_url'], "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._post({"embeds": [embed]})

    def send_run_summary(self, scraped_count: int, deals_count: int, elapsed_seconds: float):
        """Send a run summary embed at the end of each scrape."""
        minutes = int(elapsed_seconds // 60)
        seconds = int(elapsed_seconds % 60)
        embed = {
            "title": "OA Bot — Run Complete",
            "color": COLOUR_INFO,
            "fields": [
                {"name": "Products Scraped",
                 "value": str(scraped_count), "inline": True},
                {"name": "Deals Found",
                 "value": str(deals_count), "inline": True},
                {"name": "Run Time",
                 "value": f"{minutes}m {seconds}s", "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._post({"embeds": [embed]})

    def _post(self, payload: dict):
        if self.username:
            payload = {**payload, "username": self.username}
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error(f"[discord] Webhook failed: {e}")

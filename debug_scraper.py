"""
Extract full data-productdatalayer JSON from Currys category page.
Also match EAN numbers found in page HTML to product cards.
"""
import asyncio
import json
import re
from playwright.async_api import async_playwright

CATEGORY_URL = "https://www.currys.co.uk/computing/laptops/laptops"
USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

EAN_RE = re.compile(r'\b(\d{13})\b')

# Known false-positive patterns (timestamps, placeholders)
FAKE_EAN_PREFIXES = {'6666666666666', '0000000000000', '1111111111111'}


def is_likely_ean(candidate: str) -> bool:
    """Basic filter — reject obvious non-EAN 13-digit numbers."""
    if candidate in FAKE_EAN_PREFIXES:
        return False
    # Timestamps tend to start with 17... (Unix ms in 2026)
    if candidate.startswith('177') or candidate.startswith('178'):
        return False
    return True


async def debug():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        print("[1] Loading category page...")
        await page.goto(CATEGORY_URL, wait_until='domcontentloaded', timeout=60_000)

        # --- Dismiss cookie/personalisation consent popup ---
        print("  Dismissing consent popup if present...")
        consent_selectors = [
            'button:has-text("Allow all")',
            'button:has-text("Allow All")',
            'button#onetrust-accept-btn-handler',
            'button[id*="accept-all"]',
            'button[class*="accept-all"]',
            'button:has-text("Accept all")',
            'button:has-text("Accept All")',
            'button:has-text("Continue without personalising")',
            'button:has-text("Continue without")',
            '[class*="cookie"] button[class*="accept"]',
        ]
        for sel in consent_selectors:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=3000):
                    await btn.click()
                    print(f"  Clicked consent button: {sel}")
                    await asyncio.sleep(2)
                    break
            except Exception:
                continue

        await page.wait_for_load_state('networkidle', timeout=30_000)
        await asyncio.sleep(3)

        # --- Pull full data-productdatalayer for each product ---
        print("\n[2] Extracting full data-productdatalayer JSON from product cards...")
        products_raw = await page.evaluate("""() => {
            const cards = document.querySelectorAll('[data-pid][data-productdatalayer]');
            const results = [];
            const seen = new Set();
            for (const card of cards) {
                const pid = card.getAttribute('data-pid');
                if (seen.has(pid)) continue;
                seen.add(pid);

                // Get title from pdpLink anchor
                const titleEl = card.querySelector('a.pdpLink, [class*="pdpLink"]');
                const title = titleEl ? titleEl.textContent.trim() : '';

                // Get price from span.value[content] or text
                const priceEl = card.querySelector('span.value[content], span.value');
                const priceContent = priceEl
                    ? (priceEl.getAttribute('content') || priceEl.textContent.trim())
                    : '';

                // Get product URL
                const linkEl = card.querySelector('a.pdpLink, a[href*="/p/"], a[href*="/product"]');
                const url = linkEl ? linkEl.href : '';

                results.push({
                    pid,
                    title,
                    price: priceContent,
                    url,
                    datalayer_raw: card.getAttribute('data-productdatalayer'),
                });
            }
            return results;
        }""")

        print(f"  Found {len(products_raw)} products")

        # Parse and display each product's data
        products = []
        for p in products_raw:
            dl_parsed = None
            if p['datalayer_raw']:
                try:
                    dl_parsed = json.loads(p['datalayer_raw'])
                except Exception as e:
                    print(f"  PID {p['pid']}: JSON parse error: {e}")

            product = {
                'pid': p['pid'],
                'title': p['title'],
                'price_raw': p['price'],
                'url': p['url'],
                'datalayer': dl_parsed,
            }
            products.append(product)

        # Show full structure of first product
        if products and products[0]['datalayer']:
            print("\n[3] Full datalayer JSON for first product:")
            print(json.dumps(products[0]['datalayer'], indent=2)[:3000])

        # Extract price from datalayer payment array
        print("\n[4] Product summary (title / price / EAN from datalayer):")
        for p in products:
            dl = p['datalayer']
            price = None
            ean = None

            if dl and isinstance(dl, list) and dl:
                item = dl[0]
                # Price is in payment[0].amount
                payment = item.get('availability', [{}])[0].get('shipping', [{}])
                # Actually dig into the structure properly
                avail = item.get('availability', [])
                for a in avail:
                    shipping = a.get('shipping', [])
                    for s in shipping:
                        payments = s.get('payment', [])
                        for pay in payments:
                            if pay.get('frequency') in ('one off', 'oneoff', 'one_off') or pay.get('installments') == 1:
                                price = pay.get('amount')
                                break

                # Look for EAN directly in the datalayer item
                ean = (
                    item.get('ean') or item.get('gtin13') or item.get('gtin') or
                    item.get('barcode') or item.get('upc')
                )
                if isinstance(ean, list):
                    ean = ean[0] if ean else None

            print(f"  PID {p['pid']}: £{price} | EAN: {ean} | {p['title'][:60]}")

        # --- Match EANs from raw HTML to products ---
        print("\n[5] All 13-digit numbers found in HTML, filtered:")
        html = await page.content()
        all_candidates = EAN_RE.findall(html)
        filtered = [c for c in dict.fromkeys(all_candidates) if is_likely_ean(c)]
        print(f"  {len(filtered)} likely EANs: {filtered}")

        # --- Check dataLayer product events for EAN data ---
        print("\n[6] Full productDetail from dataLayer:")
        dl_products = await page.evaluate("""() => {
            const dl = window.dataLayer || [];
            for (const evt of dl) {
                if (evt.event === 'product' && evt.productDetail) {
                    return JSON.stringify(evt.productDetail).substring(0, 5000);
                }
            }
            return null;
        }""")
        if dl_products:
            print(dl_products)
        else:
            print("  No product event in dataLayer yet")

        # --- Dump keys of one datalayer item to see what fields are available ---
        if products and products[0]['datalayer'] and isinstance(products[0]['datalayer'], list):
            item = products[0]['datalayer'][0]
            print("\n[7] Top-level keys in datalayer[0] for first product:")
            print(f"  {list(item.keys())}")
            # Print any field that might be an EAN/identifier
            for key in item.keys():
                val = item[key]
                if isinstance(val, (str, int, float)) and not isinstance(val, bool):
                    print(f"  {key}: {str(val)[:100]}")

        await browser.close()


asyncio.run(debug())

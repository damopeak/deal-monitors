"""Diagnostic: check one product by Currys URL or Amazon ASIN."""
import asyncio
import json
import re
import sys

from config_loader import load_config
from keepa_client import KeepaClient
from calculator import calculate_roi
from scraper import _dismiss_consent, USER_AGENT

REFERRAL_FEE_PERCENT = 7
CATEGORY_DISCOUNT = 100
GIFT_CARD_DISCOUNT_PCT = 5


async def fetch_currys_product(url: str) -> dict | None:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, channel='chrome')
        context = await browser.new_context(user_agent=USER_AGENT)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()
        await page.goto(url, wait_until='domcontentloaded', timeout=45_000)
        await asyncio.sleep(4)
        await _dismiss_consent(page)
        await asyncio.sleep(2)

        raw = await page.evaluate("""() => {
            // 1. Category listing page — data-productdatalayer attribute
            const dlEl = document.querySelector('[data-productdatalayer]');
            if (dlEl) {
                return { source: 'datalayer', data: dlEl.getAttribute('data-productdatalayer') };
            }

            // 2. PDP — look in window.dataLayer for ecommerce.detail or ecommerce.impressions
            if (window.dataLayer) {
                for (const entry of window.dataLayer) {
                    if (entry.ecommerce) {
                        const products =
                            (entry.ecommerce.detail && entry.ecommerce.detail.products) ||
                            (entry.ecommerce.impressions) ||
                            [];
                        if (products.length > 0) {
                            return { source: 'datalayer_ecommerce', data: JSON.stringify(products[0]) };
                        }
                    }
                    // Some Currys pages push product directly
                    if (entry.productId || entry.ean || entry.product_id) {
                        return { source: 'datalayer_direct', data: JSON.stringify(entry) };
                    }
                }
            }

            // 3. JSON-LD structured data
            const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const s of ldScripts) {
                try {
                    const obj = JSON.parse(s.textContent);
                    if (obj['@type'] === 'Product' || obj.sku || obj.gtin13) {
                        return { source: 'jsonld', data: JSON.stringify(obj) };
                    }
                } catch(e) {}
            }

            // 4. Last resort — look for any script containing ean or barcode
            const scripts = document.querySelectorAll('script:not([src])');
            for (const s of scripts) {
                const t = s.textContent;
                const m = t.match(/"ean"\s*:\s*"?(\d{12,13})"?/);
                if (m) {
                    const pm = t.match(/"(?:price|salePrice|listPrice)"\s*:\s*"?([\d.]+)"?/);
                    const nm = t.match(/"(?:name|title|productName)"\s*:\s*"([^"]+)"/);
                    return { source: 'inline_script', ean: m[1], price: pm ? pm[1] : null, name: nm ? nm[1] : null };
                }
            }

            return null;
        }""")
        await browser.close()

    if not raw:
        print("ERROR: No product data found on page")
        return None

    title, ean, price = None, None, None
    source = raw.get('source', '')

    if source == 'datalayer':
        try:
            dl = json.loads(raw['data'])
            item = dl[0] if isinstance(dl, list) else dl
            title = item.get('name', '').strip()
            ean_raw = item.get('ean')
            ean = str(ean_raw[0] if isinstance(ean_raw, list) else ean_raw) if ean_raw else None
            for avail in item.get('availability', []):
                for shipping in avail.get('shipping', []):
                    for pay in shipping.get('payment', []):
                        if pay.get('frequency') == 'one off' and pay.get('installments', 0) == 1:
                            amt = pay.get('amount')
                            if amt and float(amt) > 0:
                                price = float(amt)
        except Exception as e:
            print(f"Parse error (datalayer): {e}")

    elif source == 'datalayer_ecommerce':
        try:
            item = json.loads(raw['data'])
            title = item.get('name', '').strip()
            ean = str(item.get('id') or item.get('ean') or '')
            price_str = item.get('price')
            if price_str:
                price = float(str(price_str).replace(',', ''))
        except Exception as e:
            print(f"Parse error (ecommerce): {e}")

    elif source == 'jsonld':
        try:
            item = json.loads(raw['data'])
            title = item.get('name', '').strip()
            ean = item.get('gtin13') or item.get('gtin') or item.get('sku')
            offers = item.get('offers', {})
            if isinstance(offers, list):
                offers = offers[0]
            price_str = offers.get('price')
            if price_str:
                price = float(str(price_str).replace(',', ''))
        except Exception as e:
            print(f"Parse error (jsonld): {e}")

    elif source == 'inline_script':
        ean = raw.get('ean')
        price = float(raw['price']) if raw.get('price') else None
        title = raw.get('name', '')

    if ean and not re.fullmatch(r'\d{12,13}', ean):
        ean = None

    if not title and not ean:
        print(f"ERROR: Could not extract product data (source={source})")
        print(f"Raw: {raw}")
        return None

    return {
        'title': title or '(unknown)',
        'price': price,
        'ean': ean,
        'url': url,
        'referral_fee_percent': REFERRAL_FEE_PERCENT,
        'referral_fee_tiers': None,
        'discount': CATEGORY_DISCOUNT,
    }


def show_roi(product: dict, amazon_data: dict, config: dict):
    flat_discount = product.get('discount', 0)
    price_after_flat = product['price'] - flat_discount
    gift_card_saving = round(price_after_flat * GIFT_CARD_DISCOUNT_PCT / 100, 2)
    supplier_price = price_after_flat - gift_card_saving

    print(f"\nCost breakdown:")
    print(f"  Currys price  : £{product['price']:.2f}")
    print(f"  £100 off      : -£{flat_discount:.2f}  → £{price_after_flat:.2f}")
    print(f"  5% gift card  : -£{gift_card_saving:.2f}  → £{supplier_price:.2f}")

    print(f"\nAmazon match:")
    print(f"  ASIN          : {amazon_data['asin']}")
    print(f"  Title         : {amazon_data.get('title', '')[:70]}")
    print(f"  Match type    : {amazon_data.get('match_type', 'ean')}")
    print(f"  Selling price : £{amazon_data['selling_price']:.2f} ({amazon_data['price_type']})")
    print(f"  FBA fee       : £{amazon_data.get('fba_fee', 0):.2f}")
    print(f"  BSR           : {amazon_data.get('bsr', 'N/A')}")
    print(f"  Monthly sales : {amazon_data.get('monthly_sold', 'N/A')}")
    print(f"  Amazon URL    : {amazon_data['amazon_url']}")

    deal = calculate_roi(supplier_price, amazon_data, config, product)
    min_roi = config['roi']['min_roi_percent']
    vat_mode = config['roi'].get('vat_registered', False)
    vat_mult = 1 + config['roi'].get('vat_rate', 20) / 100
    ref_pct = product.get('referral_fee_percent', config['roi']['referral_fee_percent'])

    if deal:
        print(f"\n  ✅ DEAL! Profit £{deal['profit']:.2f} | ROI {deal['roi']:.1f}%")
    else:
        if vat_mode:
            sell_ex = amazon_data['selling_price'] / vat_mult
            sup_ex = supplier_price / vat_mult
            fba = amazon_data.get('fba_fee', 0)
            ref = amazon_data['selling_price'] * ref_pct / 100
            dig = (ref + fba) * config['roi'].get('digital_services_fee_percent', 0) / 100
            prep = config['roi'].get('prep_fee_per_unit', 0)
            profit = sell_ex - sup_ex - fba - ref - dig - prep
        else:
            fba = amazon_data.get('fba_fee', 0)
            ref = amazon_data['selling_price'] * ref_pct / 100
            profit = amazon_data['selling_price'] - supplier_price - fba - ref
        roi = (profit / supplier_price) * 100
        print(f"\n  ❌ Below threshold — Profit £{profit:.2f} | ROI {roi:.1f}% (need {min_roi}%)")
        needed = amazon_data['selling_price'] * (1 + (min_roi - roi) / 100)
        print(f"  Amazon would need to be ~£{needed:.2f} to qualify")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if not arg:
        print("Usage:")
        print("  python check_product.py <currys_url>")
        print("  python check_product.py <amazon_asin>  (e.g. B0F4DLN17X)")
        return

    config = load_config()
    keepa = KeepaClient(config)

    # Direct ASIN lookup
    if re.match(r'^[A-Z0-9]{10}$', arg):
        print(f"\nLooking up ASIN {arg} on Keepa...")
        try:
            products = keepa.api.query([arg], product_code_is_asin=True, domain=keepa.domain,
                                       history=True, to_datetime=False)
            if not products or not products[0]:
                print("No Keepa data for this ASIN.")
                return
            amazon_data = keepa._parse_product(products[0])
            if not amazon_data:
                print("Keepa returned data but no current price.")
                return
            print(f"\n{'='*60}")
            print(f"ASIN   : {amazon_data['asin']}")
            print(f"Title  : {amazon_data.get('title', '')[:80]}")
            print(f"EANs   : {products[0].get('eanList', [])}")
            print(f"Price  : £{amazon_data['selling_price']:.2f} ({amazon_data['price_type']})")
            print(f"FBA fee: £{amazon_data.get('fba_fee', 0):.2f}")
            print(f"BSR    : {amazon_data.get('bsr', 'N/A')}")
            print(f"URL    : {amazon_data['amazon_url']}")
            print(f"\nEnter Currys price to calculate ROI (or press Enter to skip):")
            currys_price_str = input("> £").strip()
            if currys_price_str:
                currys_price = float(currys_price_str)
                product = {
                    'title': amazon_data.get('title', ''),
                    'price': currys_price,
                    'ean': None,
                    'url': '',
                    'referral_fee_percent': REFERRAL_FEE_PERCENT,
                    'referral_fee_tiers': None,
                    'discount': CATEGORY_DISCOUNT,
                }
                show_roi(product, amazon_data, config)
        except Exception as e:
            print(f"Error: {e}")
        return

    # Currys URL scrape
    print(f"\nFetching: {arg}")
    product = asyncio.run(fetch_currys_product(arg))
    if not product:
        return

    print(f"\n{'='*60}")
    print(f"Title : {product['title']}")
    print(f"Price : £{product['price']}" if product['price'] else "Price : NOT FOUND")
    print(f"EAN   : {product['ean'] or 'MISSING'}")

    if not product['price']:
        print("Cannot calculate ROI without price.")
        return

    if product['ean']:
        print(f"\nLooking up EAN {product['ean']} on Keepa...")
        results = list(keepa.lookup_products_by_ean_batched([product['ean']]))
        amazon_data = results[0][0] if results and results[0] else None
    else:
        print("\nNo EAN — trying title search...")
        amazon_data = keepa.lookup_by_title(product['title'])

    if not amazon_data:
        print("No Amazon match found.")
        return

    show_roi(product, amazon_data, config)
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()

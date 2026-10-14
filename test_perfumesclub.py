"""
Quick test: scrapes the first product from PerfumesClub Chanel listing
and prints title, price and EAN. Run with: python test_perfumesclub.py
"""
import asyncio
import logging
from config_loader import load_config
from scraper import Scraper, _dismiss_consent, USER_AGENT

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

LISTING_URL = "https://www.perfumesclub.co.uk/en/chanel/m/"
LINK_SEL = "a[href*='/p_']"


async def main():
    config = load_config()
    scraper = Scraper(config)

    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, channel='chrome')
        context = await browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()

        # Load listing and grab first product URL
        logger.info(f"Loading listing: {LISTING_URL}")
        await page.goto(LISTING_URL, wait_until='domcontentloaded', timeout=45_000)
        await asyncio.sleep(6)
        await _dismiss_consent(page)

        # Scroll to trigger lazy-loaded products
        for scroll_y in [600, 1200, 1800]:
            await page.evaluate(f"window.scrollTo(0, {scroll_y})")
            await asyncio.sleep(2)

        # Debug: show all distinct href patterns on the page
        all_links = await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href]'))
                .map(a => a.getAttribute('href'))
                .filter(h => h && h.includes('/en/'))
                .slice(0, 20);
        }""")
        logger.info(f"Sample /en/ hrefs on page: {all_links}")

        product_urls = await page.evaluate("""(sel) => {
            const els = document.querySelectorAll(sel);
            const seen = new Set();
            const out = [];
            for (const el of els) {
                const href = el.href;
                if (href && !seen.has(href)) { seen.add(href); out.push(href); }
            }
            return out;
        }""", LINK_SEL)

        logger.info(f"Found {len(product_urls)} product links on listing page")

        # Debug: try clicking "SEE MORE ITEMS" once and see what changes
        before_count = len(product_urls)
        btn_sel = "a:has-text('SEE MORE ITEMS'), button:has-text('SEE MORE ITEMS')"
        try:
            btn = page.locator(btn_sel).first
            if await btn.is_visible(timeout=3000):
                logger.info("Found SEE MORE ITEMS button — clicking...")
                await btn.click()
                await asyncio.sleep(4)
                after_urls = await page.evaluate("""(sel) => {
                    const els = document.querySelectorAll(sel);
                    return [...new Set(Array.from(els).map(a => a.href))];
                }""", LINK_SEL)
                logger.info(f"After click: {len(after_urls)} product links (was {before_count})")
                # Show sample of ALL link patterns on page to check if new products use different URLs
                all_hrefs = await page.evaluate("""() =>
                    [...new Set(Array.from(document.querySelectorAll('a[href]'))
                        .map(a => a.getAttribute('href'))
                        .filter(h => h && h.startsWith('/en/') && h.includes('/') && h.length > 10)
                    )].slice(0, 30)
                """)
                logger.info(f"Sample hrefs after click: {all_hrefs}")
                product_urls = after_urls
            else:
                logger.info("SEE MORE ITEMS button NOT visible")
        except Exception as e:
            logger.error(f"Button click failed: {e}")

        if not product_urls:
            logger.error("No product links found — check LINK_SEL or the listing URL")
            await browser.close()
            return

        test_url = product_urls[0]
        logger.info(f"Testing product: {test_url}")

        result = await scraper._extract_product_page(
            page, test_url, "PerfumesClub", 15, None, 0
        )

        print("\n" + "=" * 60)
        if result:
            print(f"Title : {result['title']}")
            print(f"Price : £{result['price']}")
            print(f"EAN   : {result['ean'] or 'NOT FOUND'}")
            print(f"URL   : {result['url']}")
        else:
            print("FAILED — could not extract title or price from product page")
        print("=" * 60)

        input("\nPress Enter to close the browser...")
        await browser.close()


asyncio.run(main())

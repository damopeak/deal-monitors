import asyncio
import json
import logging
import random
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

NEXT_PAGE_SELECTORS = [
    'a[rel="next"]',
    'a[aria-label="Next"]', 'a[aria-label="next page"]',
    '.pagination .next a', '.next-page a', 'a.next',
    '[class*="pagination"] a[class*="next"]',
]

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/124.0.0.0 Safari/537.36'
)

# Consent popup button selectors — tried in order
CONSENT_SELECTORS = [
    'button:has-text("Allow all")',
    'button:has-text("Allow All")',
    'button#onetrust-accept-btn-handler',
    'button[id*="accept-all"]',
    'button:has-text("Accept all")',
    'button:has-text("Accept All")',
    'button:has-text("Continue without personalising")',
]


async def _dismiss_consent(page):
    """Click the cookie/consent popup if it appears."""
    # Wait up to 8s for any consent container to appear
    try:
        await page.wait_for_selector(
            '#onetrust-banner-sdk, #onetrust-accept-btn-handler, '
            '[class*="cookie-banner"], [class*="consent-banner"]',
            timeout=8000
        )
    except Exception:
        logger.debug("[scraper] No consent popup detected")
        return

    async def _click_and_wait(btn, label):
        """Click a consent button and wait for the resulting page reload."""
        await btn.click()
        logger.info(f"[scraper] Dismissed consent popup ({label})")
        # Consent click often triggers a page reload — wait for it
        try:
            await page.wait_for_load_state('domcontentloaded', timeout=10_000)
        except Exception:
            pass
        await asyncio.sleep(2)

    # Try known button selectors
    for sel in CONSENT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await _click_and_wait(btn, sel)
                return
        except Exception:
            continue

    # Last resort: find any button whose text contains "allow"
    try:
        buttons = page.locator('button')
        count = await buttons.count()
        for i in range(count):
            btn = buttons.nth(i)
            text = (await btn.text_content() or '').strip()
            if 'allow' in text.lower():
                await _click_and_wait(btn, f'text="{text}"')
                return
    except Exception:
        pass

    logger.warning("[scraper] Consent popup found but could not click it")


class Scraper:
    def __init__(self, config):
        self.config = config
        self.max_pages = config['scraper']['max_pages_per_category']
        self.delay = config['scraper'].get('request_delay_seconds', 2)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def scrape_all_sites(self, on_category=None) -> list[dict]:
        """Scrape every enabled site.

        on_category: optional callable(site_name, category_name, products) invoked as
        soon as each category finishes, so callers can act on results during the sweep
        instead of waiting for all of it. Exceptions in the callback are logged and
        never abort the sweep.
        """
        all_products = []
        self._on_category = on_category

        # Build proxy dict if configured
        proxy_cfg = self.config.get('scraper', {}).get('proxy')
        proxy = None
        if proxy_cfg and not proxy_cfg.get('server', '').startswith('http://PROXY'):
            proxy = {
                'server': proxy_cfg['server'],
                'username': proxy_cfg.get('username'),
                'password': proxy_cfg.get('password'),
            }
            logger.info(f"[scraper] Using proxy: {proxy_cfg['server']}")
        else:
            logger.info("[scraper] No proxy configured — using direct connection")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.config.get('scraper', {}).get('headless', False),
                channel='chrome',
            )

            # concurrency 1 (the default) preserves the original sequential behaviour
            concurrency = max(1, int(self.config.get('scraper', {}).get('concurrency', 1)))
            sem = asyncio.Semaphore(concurrency)
            if concurrency > 1:
                logger.info(f"[scraper] Scraping {concurrency} categories concurrently")

            jobs = []
            cat_index = 0
            for site in self.config['sites']:
                if not site.get('enabled', True):
                    logger.info(f"[scraper] Site: {site['name']} (disabled — skipping)")
                    continue
                selectors = site.get('selectors') or {}
                logger.info(f"[scraper] Site: {site['name']}")

                for category in site.get('categories', []):
                    jobs.append(self._run_category(
                        browser, site, category, selectors, proxy, cat_index, sem
                    ))
                    cat_index += 1

            for products in await asyncio.gather(*jobs):
                all_products.extend(products)

            await browser.close()

        return all_products

    async def _run_category(self, browser, site, category, selectors,
                            proxy, cat_index, sem) -> list[dict]:
        """Scrape one category in its own browser context."""
        async with sem:
            cat_name = category.get('name', category['url'])
            logger.info(f"[scraper]   Category: {cat_name}")

            # Fresh context + new session ID per category = fresh IP each time
            # Sites can opt out of proxy with use_proxy: false in config
            cat_proxy = None
            if proxy and site.get('use_proxy', True):
                cat_proxy = {
                    'server': proxy['server'],
                    'username': f"{proxy['username'].split('-sessid-')[0]}-sessid-cat{cat_index}",
                    'password': proxy['password'],
                }
                logger.info(f"[scraper]   Proxy session: {cat_proxy['username']}")
            elif not site.get('use_proxy', True):
                logger.info("[scraper]   Proxy disabled for this site — direct connection")

            context = await browser.new_context(
                user_agent=USER_AGENT,
                proxy=cat_proxy,
            )
            # Currys serves the category page to an automated browser but
            # silently 403s the search-update-grid pagination XHR unless
            # navigator.webdriver is masked.
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', "
                "{get: () => undefined})"
            )
            page = await context.new_page()

            products: list[dict] = []
            try:
                if site.get('scrape_mode') == 'product_pages':
                    products = await self._scrape_product_pages_category(
                        page, site, category, selectors
                    )
                else:
                    products, _ = await self._scrape_category(
                        page, site, category, selectors, False
                    )
                logger.info(f"[scraper]   Found {len(products)} products in {cat_name}")
            except Exception as e:
                logger.error(f"[scraper]   Error in {cat_name}: {e}")
            finally:
                await context.close()

            # Hand results over immediately so callers can act on this category while
            # the rest of the sweep continues. Must never break the sweep.
            cb = getattr(self, '_on_category', None)
            if cb and products:
                try:
                    cb(site['name'], cat_name, products)
                except Exception:
                    logger.exception(f"[scraper]   on_category callback failed for {cat_name}")

            await asyncio.sleep(random.uniform(5, 10))
            return products

    # ------------------------------------------------------------------
    # Category page — extracts all products directly from the page
    # No product page visits needed: all data lives in data-productdatalayer
    # ------------------------------------------------------------------

    async def _scrape_category(self, page, site, category, selectors, consent_done: bool) -> tuple[list[dict], bool]:
        products = []
        current_url = category['url']
        referral_fee_percent = category.get('referral_fee_percent')
        referral_fee_tiers = category.get('referral_fee_tiers')
        discount = category.get('discount', 0)
        discount_percent = category.get('discount_percent', 0)

        seen_pids: set = set()
        first_page = True

        for page_num in range(1, self.max_pages + 1):
            logger.info(f"[scraper]   Page {page_num}: {current_url}")

            try:
                if first_page:
                    await page.goto(current_url, wait_until='domcontentloaded', timeout=45_000)
                    first_page = False

                    await asyncio.sleep(4)
                    page_title = await page.title()
                    if 'Attention Required' in page_title or 'Cloudflare' in page_title:
                        logger.warning(f"[scraper]   Cloudflare block on page {page_num} — skipping category")
                        break

                    if not consent_done:
                        await _dismiss_consent(page)
                        consent_done = True

                # Scroll to trigger lazy-loaded JS
                await page.evaluate("window.scrollTo(0, 800)")
                await asyncio.sleep(2)
                await page.evaluate("window.scrollTo(0, 1600)")
                await asyncio.sleep(2)

                try:
                    await page.wait_for_selector('[data-productdatalayer]', timeout=40_000)
                except Exception:
                    logger.debug("[scraper]   wait_for_selector timed out — attempting extraction anyway")

                await asyncio.sleep(self.delay)

            except Exception as e:
                logger.error(f"[scraper]   Page load error on page {page_num}: {e}")
                break

            # Extract products from data-productdatalayer attributes on this page
            page_products = await self._extract_from_datalayer(
                page, site['name'], referral_fee_percent, referral_fee_tiers,
                discount, discount_percent
            )

            if not page_products:
                logger.warning(f"[scraper]   No products on page {page_num} — stopping")
                break

            # Deduplicate against previously seen PIDs — if all PIDs already seen, pagination looped
            new_products = [p for p in page_products if p['url'] not in seen_pids]
            if not new_products:
                logger.info(f"[scraper]   Page {page_num}: all products already seen — pagination complete")
                break
            for p in new_products:
                seen_pids.add(p['url'])

            logger.info(f"[scraper]   Page {page_num}: {len(new_products)} new products extracted")
            products.extend(new_products)

            # Pagination — click Next button (AJAX pagination, no URL change)
            next_sel = selectors.get('next_page', 'a.page-next')
            all_next_sels = [next_sel] + [
                'a[rel="next"]', 'a[aria-label="Next"]',
                '.pagination .next a', 'a.next',
            ]
            # Signature of the product grid before clicking, so we can tell when the
            # AJAX swap has actually landed. Uses data-productdatalayer rather than
            # data-pid: data-pid also covers carousel tiles, which shuffle on their
            # own and cause a false "changed" reading before the new grid arrives.
            grid_sig = """() => Array.from(
                document.querySelectorAll('[data-productdatalayer]'))
                .map(el => el.getAttribute('data-productdatalayer').slice(-40))
                .join(',')"""
            current_pids = await page.evaluate(grid_sig)

            clicked = False
            for sel in all_next_sels:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        # The link must be scrolled into view or the click silently
                        # fails to trigger the search-update-grid XHR
                        await btn.scroll_into_view_if_needed(timeout=5000)
                        await asyncio.sleep(1)
                        await btn.click()
                        # Wait for the product grid itself to change (AJAX reload)
                        changed = False
                        for _ in range(20):
                            await asyncio.sleep(1)
                            new_pids = await page.evaluate(grid_sig)
                            if new_pids != current_pids:
                                changed = True
                                break
                        if not changed:
                            logger.warning(
                                f"[scraper]   Clicked {sel!r} but the grid never "
                                f"changed — pagination may be blocked"
                            )
                        await asyncio.sleep(2)  # let the new grid settle before extracting
                        current_url = page.url
                        clicked = True
                        break
                except Exception:
                    continue

            if not clicked:
                logger.info(f"[scraper]   No next page button — done with category")
                break

        return products, consent_done

    # ------------------------------------------------------------------
    # Extract product data from data-productdatalayer attributes
    # ------------------------------------------------------------------

    async def _extract_from_datalayer(
        self, page, site_name: str,
        referral_fee_percent, referral_fee_tiers, discount=0, discount_percent=0
    ) -> list[dict]:
        """
        Read data-productdatalayer JSON from each product card.
        Returns a list of product dicts with title, price, ean, url.
        """
        raw_items = await page.evaluate("""() => {
            // Find all elements that have data-productdatalayer (may or may not also have data-pid)
            const dlEls = document.querySelectorAll('[data-productdatalayer]');
            const results = [];
            const seen = new Set();

            for (const el of dlEls) {
                // data-pid may be on the same element, a parent, or a sibling container
                const pidEl = el.closest('[data-pid]') || el.querySelector('[data-pid]');
                const pid = el.getAttribute('data-pid') || (pidEl ? pidEl.getAttribute('data-pid') : '');

                const datalayer_raw = el.getAttribute('data-productdatalayer');
                if (!datalayer_raw) continue;

                // Deduplicate by pid, falling back to a slice near the end of the JSON
                const dedupeKey = pid || datalayer_raw.substring(datalayer_raw.length - 40);
                if (seen.has(dedupeKey)) continue;
                seen.add(dedupeKey);

                const container = el.closest('li, article, [class*="product"], [class*="tile"]') || el;
                const linkEl = container.querySelector('a.pdpLink, a[href*="/p/"], a[href*="/product"]')
                    || document.querySelector(`a[href*="${pid}"]`);
                const url = linkEl ? linkEl.href : '';

                // Product image — used downstream to verify fuzzy title matches
                // against the Amazon listing image (see image_match.compare_images).
                const imgEl = container.querySelector('img');
                let imgUrl = '';
                if (imgEl) {
                    imgUrl = imgEl.getAttribute('data-src')
                          || imgEl.getAttribute('data-lazy-src')
                          || imgEl.getAttribute('data-original')
                          || imgEl.getAttribute('src') || '';
                    if (imgUrl.startsWith('data:') || imgUrl.includes('placeholder')) imgUrl = '';
                }

                results.push({ pid, url, datalayer_raw, imgUrl });
            }
            return results;
        }""")

        products = []
        for item_raw in raw_items:
            if not item_raw.get('datalayer_raw'):
                continue

            try:
                dl = json.loads(item_raw['datalayer_raw'])
            except Exception as e:
                logger.debug(f"[scraper] JSON parse error for PID {item_raw.get('pid')}: {e}")
                continue

            if not isinstance(dl, list) or not dl:
                continue

            item = dl[0]

            # --- Title ---
            title = item.get('name', '').strip()
            if not title:
                continue

            # --- EAN ---
            ean_raw = item.get('ean')
            if isinstance(ean_raw, list):
                ean = str(ean_raw[0]) if ean_raw else None
            elif ean_raw:
                ean = str(ean_raw)
            else:
                ean = None

            # Validate: accept 12-digit UPC or 13-digit EAN
            if ean and not re.fullmatch(r'\d{12,13}', ean):
                ean = None

            # --- Price: one-off payment from availability > shipping > payment ---
            price = self._extract_price_from_datalayer(item)
            if price is None or price <= 0:
                logger.debug(f"[scraper] No price for {title[:50]} — skipping")
                continue

            products.append({
                'title': title,
                'price': price,
                'ean': ean,
                'url': item_raw['url'],
                'img_url': item_raw.get('imgUrl', ''),
                'site_name': site_name,
                'referral_fee_percent': referral_fee_percent,
                'referral_fee_tiers': referral_fee_tiers,
                'discount': discount,
                'discount_percent': discount_percent,
            })

        return products

    @staticmethod
    def _extract_price_from_datalayer(item: dict) -> float | None:
        """
        Extract the one-off selling price from the Currys datalayer structure:
          availability[] > shipping[] > payment[] where frequency == 'one off'
        Amount is already in whole pounds (e.g. 599 = £599).
        """
        for avail in item.get('availability', []):
            for shipping in avail.get('shipping', []):
                for pay in shipping.get('payment', []):
                    if pay.get('frequency') == 'one off' and pay.get('installments', 0) == 1:
                        amount = pay.get('amount')
                        if amount and float(amount) > 0:
                            return float(amount)
        return None

    # ------------------------------------------------------------------
    # Product-pages mode — visit each product page individually
    # Used for sites like PerfumesClub where listing has no structured data
    # ------------------------------------------------------------------

    async def _scrape_product_pages_category(
        self, page, site, category, selectors
    ) -> list[dict]:
        products = []
        listing_url = category['url']
        referral_fee_percent = category.get('referral_fee_percent')
        referral_fee_tiers = category.get('referral_fee_tiers')
        discount = category.get('discount', 0)
        discount_percent = category.get('discount_percent', 0)

        logger.info(f"[scraper]   Loading listing: {listing_url}")
        await page.goto(listing_url, wait_until='domcontentloaded', timeout=45_000)
        await asyncio.sleep(4)

        page_title = await page.title()
        if 'Attention Required' in page_title or 'Cloudflare' in page_title:
            logger.warning("[scraper]   Cloudflare block on listing — skipping category")
            return products

        await _dismiss_consent(page)

        # Click "load more" until all products are visible (up to 15 clicks)
        load_more_sels = list(filter(None, [
            selectors.get('load_more', ''),
            '.js-load-more', 'a.js-load-more', '[class*="load-more"]',
            'button:has-text("See more")', 'a:has-text("See more")',
            'button:has-text("Load more")', 'a:has-text("Load more")',
        ]))
        link_sel = selectors.get('product_links', "a[href*='/p_']")
        prev_count = 0
        for click_num in range(15):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)
            clicked = False
            for sel in load_more_sels:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        await asyncio.sleep(3)
                        count = await page.evaluate(
                            "(sel) => new Set(Array.from(document.querySelectorAll(sel)).map(a => a.href)).size",
                            link_sel
                        )
                        logger.info(f"[scraper]   Load more #{click_num + 1} — {count} products so far")
                        if count <= prev_count:
                            logger.info("[scraper]   Product count not increasing — stopping load more")
                            clicked = False
                        else:
                            prev_count = count
                            clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                logger.info("[scraper]   Listing fully loaded")
                break

        # Collect all product URLs from the listing
        link_sel = selectors.get('product_links', "a[href*='/p_']")
        product_urls = await page.evaluate("""(sel) => {
            const els = document.querySelectorAll(sel);
            const seen = new Set();
            const out = [];
            for (const el of els) {
                const href = el.href;
                if (href && !seen.has(href)) {
                    seen.add(href);
                    out.push(href);
                }
            }
            return out;
        }""", link_sel)

        logger.info(f"[scraper]   Collected {len(product_urls)} product URLs")

        # Visit each product page
        for i, url in enumerate(product_urls, 1):
            try:
                product = await self._extract_product_page(
                    page, url, site['name'],
                    referral_fee_percent, referral_fee_tiers, discount
                )
                if product:
                    products.append(product)
                    logger.debug(
                        f"[scraper]   [{i}/{len(product_urls)}] "
                        f"{product['title'][:50]} £{product['price']} EAN:{product['ean']}"
                    )
            except Exception as e:
                logger.error(f"[scraper]   [{i}/{len(product_urls)}] Error on {url}: {e}")
            await asyncio.sleep(random.uniform(self.delay, self.delay + 2))

        return products

    async def _extract_product_page(
        self, page, url: str, site_name: str,
        referral_fee_percent, referral_fee_tiers, discount: float
    ) -> dict | None:
        await page.goto(url, wait_until='domcontentloaded', timeout=30_000)
        await asyncio.sleep(2)

        # Click FEATURES tab — EAN lives there on PerfumesClub
        for tab_sel in [
            'a:has-text("FEATURES")', 'button:has-text("FEATURES")',
            '[data-tab*="feature"]', '[href*="feature"]',
        ]:
            try:
                tab = page.locator(tab_sel).first
                if await tab.is_visible(timeout=2000):
                    await tab.click()
                    await asyncio.sleep(1)
                    break
            except Exception:
                continue

        # Title — h1 text normalised + subtitle appended (e.g. "Eau de Parfum spray for woman")
        title = await page.evaluate("""() => {
            const h1 = document.querySelector('h1.titleProduct, h1[class*="title"], h1');
            if (!h1) return null;
            let text = h1.innerText.trim().replace(/\\s+/g, ' ');
            const next = h1.nextElementSibling;
            if (next && next.tagName !== 'H1' && next.innerText) {
                const sub = next.innerText.trim().replace(/\\s+/g, ' ');
                if (sub.length > 0 && sub.length < 120) text += ' ' + sub;
            }
            return text;
        }""")
        if not title:
            return None

        # EAN — JSON-LD first, then regex on page source
        ean = await self._extract_ean_jsonld(page)
        if not ean:
            ean = await self._extract_ean_from_page(page)

        # Price
        price = await self._extract_price_from_page(page)
        if not price:
            return None

        return {
            'title': title,
            'price': price,
            'ean': ean,
            'url': url,
            'site_name': site_name,
            'referral_fee_percent': referral_fee_percent,
            'referral_fee_tiers': referral_fee_tiers,
            'discount': discount,
        }

    async def _extract_ean_jsonld(self, page) -> str | None:
        scripts = await page.evaluate("""() =>
            Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                .map(s => s.textContent)
        """)
        for script in scripts:
            try:
                data = json.loads(script)
                if isinstance(data, list):
                    data = data[0] if data else {}
                for field in ('gtin13', 'gtin', 'gtin12'):
                    val = str(data.get(field) or '')
                    if re.fullmatch(r'\d{12,13}', val):
                        return val
            except Exception:
                pass
        return None

    async def _extract_ean_from_page(self, page) -> str | None:
        # DOM traversal: find element whose text is exactly "EAN", read adjacent value
        ean = await page.evaluate("""() => {
            for (const el of document.querySelectorAll('td, dt, th, strong, b, span, div, li')) {
                if (el.childElementCount === 0 && el.textContent.trim() === 'EAN') {
                    const sibling = el.nextElementSibling;
                    if (sibling) {
                        const m = sibling.textContent.match(/\\d{12,13}/);
                        if (m) return m[0];
                    }
                    const parentNext = el.parentElement && el.parentElement.nextElementSibling;
                    if (parentNext) {
                        const m = parentNext.textContent.match(/\\d{12,13}/);
                        if (m) return m[0];
                    }
                }
            }
            // Fallback: rendered (tag-free) innerText regex
            const m = document.body.innerText.match(/\\bEAN\\b[\\s:]*?(\\d{12,13})/);
            return m ? m[1] : null;
        }""")
        if ean:
            return ean
        # GTIN in JSON blobs or data attributes
        try:
            content = await page.content()
            m = re.search(r'"gtin(?:13)?"\s*:\s*"?(\d{12,13})"?', content, re.IGNORECASE)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    async def _extract_price_from_page(self, page) -> float | None:
        # Microdata content attribute (most reliable)
        try:
            val = await page.locator('[itemprop="price"]').first.get_attribute(
                'content', timeout=2000
            )
            if val:
                price = float(re.sub(r'[^\d.]', '', val))
                if price > 0:
                    return price
        except Exception:
            pass

        # PerfumesClub: clone the active priceC div, remove the pricePer100 child,
        # then take the minimum of the remaining £ values (= sale price, not RRP or per-unit)
        price = await page.evaluate("""() => {
            const container = document.querySelector('[class*="priceC"][class*="active"]');
            if (!container) return null;
            const clone = container.cloneNode(true);
            const per100 = clone.querySelector('[class*="pricePer100"]');
            if (per100) per100.remove();
            const text = clone.innerText || clone.textContent;
            const matches = [...text.matchAll(/£\\s*([\\d,]+\\.?\\d*)/g)];
            const prices = matches.map(m => parseFloat(m[1].replace(',', ''))).filter(p => p > 0);
            return prices.length ? Math.min(...prices) : null;
        }""")
        if price:
            return float(price)

        # Generic class-based fallbacks
        for sel in [
            '[class*="newPrice"]', '[class*="new-price"]', '[class*="price-new"]',
            '[class*="current-price"]', '[class*="price-current"]',
            '[class*="selling-price"]', '[class*="price-now"]',
        ]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1000):
                    text = await el.text_content() or ''
                    m = re.search(r'(\d[\d,]*\.?\d*)', text.replace(',', ''))
                    if m:
                        price = float(m.group(1))
                        if 0 < price < 10000:
                            return price
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    def _find_next_page(self, soup: BeautifulSoup, current_url: str, selectors: dict) -> str | None:
        all_selectors = (
            [selectors['next_page']] if selectors.get('next_page') else []
        ) + NEXT_PAGE_SELECTORS
        for selector in all_selectors:
            el = soup.select_one(selector)
            if el:
                href = el.get('href')
                if href:
                    return urljoin(current_url, href)
        return None

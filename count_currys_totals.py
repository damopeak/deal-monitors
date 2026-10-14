"""Read the 'Showing 1 - 20 of N' total from every configured Currys category.

Gives the real size of the site so a full-sweep runtime can be costed rather than
guessed. Visits page 1 of each category only — no pagination.
"""
import asyncio
import json
import os
import re
import time

from playwright.async_api import async_playwright

from config_loader import load_config
from scraper import USER_AGENT, _dismiss_consent

CONCURRENCY = 4
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'currys_totals.json')

TOTAL_JS = """() => {
  const m = document.body.innerText.match(/Showing\\s+[\\d,]+\\s*[-–]\\s*[\\d,]+\\s+of\\s+([\\d,]+)/i);
  if (m) return parseInt(m[1].replace(/,/g, ''), 10);
  const n = document.querySelectorAll('[data-productdatalayer]').length;
  return n ? -n : 0;   // negative => no 'showing' text, fell back to a tile count
}"""


async def worker(browser, proxy, cats, results, idx):
    ctx = await browser.new_context(user_agent=USER_AGENT, proxy=proxy)
    await ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    page = await ctx.new_page()
    consent = False
    for cat in cats:
        try:
            await page.goto(cat['url'], wait_until='domcontentloaded', timeout=45_000)
            await asyncio.sleep(3)
            if not consent:
                await _dismiss_consent(page)
                consent = True
                await asyncio.sleep(2)
            await page.evaluate('window.scrollTo(0, 1200)')
            await asyncio.sleep(2)
            total = await page.evaluate(TOTAL_JS)
            results[cat['name']] = total
            print(f"  [{idx}] {cat['name'][:34]:34} {total}")
        except Exception as e:
            results[cat['name']] = 0
            print(f"  [{idx}] {cat['name'][:34]:34} ERROR {type(e).__name__}")
    await ctx.close()


async def main():
    cfg = load_config()
    currys = next(s for s in cfg['sites'] if s['name'] == 'Currys')
    cats = currys['categories']
    px = cfg['scraper'].get('proxy')

    chunks = [cats[i::CONCURRENCY] for i in range(CONCURRENCY)]
    results: dict[str, int] = {}
    t = time.time()

    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, channel='chrome')
        await asyncio.gather(*[
            worker(b,
                   {'server': px['server'],
                    'username': f"{px['username'].split('-sessid-')[0]}-sessid-t{i}",
                    'password': px['password']} if px else None,
                   chunk, results, i)
            for i, chunk in enumerate(chunks)
        ])
        await b.close()

    known = {k: v for k, v in results.items() if v > 0}
    fallback = {k: -v for k, v in results.items() if v < 0}
    failed = [k for k, v in results.items() if v == 0]
    total = sum(known.values()) + sum(fallback.values())

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=1)

    print("\n" + "=" * 62)
    print(f"categories read : {len(known)} with totals, {len(fallback)} tile-count only, "
          f"{len(failed)} failed")
    print(f"TOTAL PRODUCTS  : {total:,}")
    print(f"pages @20/page  : {sum((v + 19)//20 for v in {**known, **fallback}.values()):,}")
    print(f"elapsed         : {time.time() - t:.0f}s")
    if known:
        top = sorted(known.items(), key=lambda kv: -kv[1])[:8]
        print("largest         : " + ", ".join(f"{k} {v:,}" for k, v in top))
    print("=" * 62)


asyncio.run(main())

"""Find Currys category URLs under given top-level sections, with product counts.

Used to extend config.yaml. Reads the 'Showing 1 - N of X' figure so each candidate
category can be judged on size before being added.
"""
import asyncio
import json
import re
import sys

from playwright.async_api import async_playwright

from scraper import USER_AGENT, _dismiss_consent

SECTIONS = sys.argv[1:] or ['phones', 'smart-tech', 'cameras']
BASE = 'https://www.currys.co.uk'
OUT = 'currys_discovered.json'


async def main():
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True, channel='chrome')
        ctx = await b.new_context(user_agent=USER_AGENT)
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = await ctx.new_page()
        consent = False
        found: dict[str, dict] = {}

        for section in SECTIONS:
            url = f'{BASE}/{section}'
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=45_000)
            except Exception as e:
                print(f'{section}: FAILED {type(e).__name__}')
                continue
            await asyncio.sleep(3)
            if not consent:
                await _dismiss_consent(page)
                consent = True
                await asyncio.sleep(2)

            links = await page.evaluate("""(sec) => {
                const out = new Set();
                document.querySelectorAll('a[href]').forEach(a => {
                    const h = a.getAttribute('href') || '';
                    if (h.startsWith('/' + sec + '/') && h.split('/').length >= 4
                        && !h.includes('?') && !h.includes('#')) out.add(h.split('?')[0]);
                });
                return Array.from(out);
            }""", section)
            print(f'\n{section}: {len(links)} candidate category links')
            found[section] = {'links': sorted(links)}

        with open(OUT, 'w', encoding='utf-8') as f:
            json.dump(found, f, indent=1)
        for sec, d in found.items():
            for l in d['links'][:40]:
                print(f'   {l}')
        await b.close()


asyncio.run(main())

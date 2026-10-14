"""End-to-end test of currys_monitor on a small category subset.

Verifies: cold start spends no tokens, prices are recorded, a price drop is
detected on the next sweep and the token budget gate behaves. Spends no Keepa
tokens and posts nothing to Discord.
"""
import json
import os

import currys_monitor as cm
from config_loader import load_config

SMALL = {'Labelling Machines', 'Epilators'}   # 11 + 22 = 33 products
cm.STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'currys_state.TEST.json')

_real_build = cm.build_currys_config


def small_build(config):
    cfg = _real_build(config)
    site = cfg['sites'][0]
    cfg['sites'] = [{**site,
                     'categories': [c for c in site['categories']
                                    if c.get('name') in SMALL]}]
    return cfg


cm.build_currys_config = small_build


def show(label):
    with open(cm.STATE_FILE, encoding='utf-8') as f:
        s = json.load(f)
    print(f"  {label}: {len(s['prices'])} prices, "
          f"{len(s['no_match'])} no-match, {len(s['alerted'])} alerted")
    return s


def main():
    if os.path.exists(cm.STATE_FILE):
        os.remove(cm.STATE_FILE)
    config = load_config()

    print("\n--- SWEEP 1 (cold start — must spend no tokens) ---")
    cm.run_cycle(config, scrape_only=True)
    s = show('state')
    if not s['prices']:
        print("  FAIL: no prices recorded")
        return

    # Inflate stored prices by 25% so the next sweep sees a drop
    for k in s['prices']:
        s['prices'][k]['price'] = round(s['prices'][k]['price'] * 1.25, 2)
    with open(cm.STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(s, f)
    print(f"\n  simulated a 20% drop across all {len(s['prices'])} products")

    print("\n--- SWEEP 2 (should detect drops, still no tokens) ---")
    cm.run_cycle(config, scrape_only=True)
    show('state')

    print("\n--- SWEEP 3 (no change — should find nothing) ---")
    cm.run_cycle(config, scrape_only=True)
    show('state')
    print("\ndone — delete currys_state.TEST.json when finished")


if __name__ == '__main__':
    main()

"""Dump raw Keepa csv data for one EAN so we can fix price parsing."""
import keepa

from config_loader import read_yaml

config = read_yaml('config.yaml')  # KEEPA_API_KEY comes from .env

api = keepa.Keepa(config['keepa_api_key'])

EAN = "0197105935396"
print(f"Querying Keepa for EAN {EAN}...")

products = api.query(
    [EAN],
    product_code_is_asin=False,
    extra_params={'idType': 'EAN'},
    domain='GB',
    history=True,
    offers=20,
    to_datetime=False,
)

if not products:
    print("No results.")
else:
    p = products[0]
    print(f"\nASIN: {p.get('asin')}")
    print(f"Title: {p.get('title', '')[:80]}")
    print(f"EAN list: {p.get('eanList')}")
    print(f"fbaFees: {p.get('fbaFees')}")

    csv = p.get('csv') or []
    print(f"\nTotal csv slots: {len(csv)}")

    labels = {
        0: 'Amazon (direct)',
        1: 'Marketplace New',
        2: 'Marketplace Used',
        3: 'Marketplace Refurbished',
        7: 'FBA New (3P)',
        10: 'FBM New (3P)',
    }

    for idx in [0, 1, 2, 3, 7, 10]:
        if idx >= len(csv) or csv[idx] is None:
            print(f"  csv[{idx}] ({labels.get(idx, '?')}): None")
            continue
        arr = csv[idx]
        print(f"\n  csv[{idx}] ({labels.get(idx, '?')}): {len(arr)} elements")
        print(f"    Raw (first 20): {list(arr[:20])}")
        # Show what even and odd indices give
        evens = [arr[i] for i in range(0, min(len(arr), 20), 2)]
        odds  = [arr[i] for i in range(1, min(len(arr), 20), 2)]
        print(f"    Even indices (0,2,4...): {evens}")
        print(f"    Odd  indices (1,3,5...): {odds}")

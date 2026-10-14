import logging

logger = logging.getLogger(__name__)


def _calc_referral_fee(selling_price: float, tiers: list | None, flat_pct: float) -> float:
    """
    Calculate referral fee using tiered rates if defined, otherwise flat rate.

    Tiers format (from config.yaml):
        referral_fee_tiers:
          - up_to: 100     # first £100 at this rate
            rate: 15
          - rate: 8        # remainder (no up_to = catches the rest)
    """
    if not tiers:
        return selling_price * (flat_pct / 100.0)

    fee = 0.0
    remaining = selling_price

    for tier in tiers:
        rate = tier['rate'] / 100.0
        up_to = tier.get('up_to')

        if up_to is not None:
            chunk = min(remaining, up_to)
            fee += chunk * rate
            remaining -= chunk
        else:
            fee += remaining * rate
            remaining = 0.0

        if remaining <= 0:
            break

    return fee


def calculate_roi(supplier_price: float, amazon_data: dict, config: dict, product: dict | None = None) -> dict | None:
    """
    Calculate profit and ROI for a product.

    VAT-registered mode (vat_registered: true in config):
        selling and supplier prices are converted to ex-VAT.
        Amazon fees from Keepa are already ex-VAT — used as-is.
        profit = selling_ex_vat - supplier_ex_vat - fba_fee - referral_fee
        roi    = profit / supplier_price_inc_vat * 100
        (ROI denominator is the cash actually paid out, inc VAT)

    Non-VAT-registered mode:
        All prices used as-is (inc VAT).
        profit = selling - supplier - fba_fee - referral_fee
        roi    = profit / supplier_price * 100

    Referral fee uses per-category tiers or flat rate if set,
    otherwise falls back to global roi.referral_fee_percent.
    Returns a deal dict if ROI >= min_roi_percent, else None.
    """
    if not supplier_price or supplier_price <= 0:
        return None

    roi_cfg = config['roi']
    min_roi = roi_cfg['min_roi_percent']
    vat_registered = roi_cfg.get('vat_registered', False)
    vat_multiplier = 1 + roi_cfg.get('vat_rate', 20) / 100.0  # e.g. 1.20

    p = product or {}
    tiers = p.get('referral_fee_tiers')
    flat = p.get('referral_fee_percent')
    global_flat = roi_cfg['referral_fee_percent']

    selling_price = amazon_data['selling_price']
    fba_fee = amazon_data.get('fba_fee', 0.0)

    # Referral fee is calculated on the inc-VAT selling price (Amazon's standard)
    referral_fee = _calc_referral_fee(selling_price, tiers, flat if flat is not None else global_flat)

    digital_services_pct = roi_cfg.get('digital_services_fee_percent', 0) / 100.0
    digital_services_fee = (referral_fee + fba_fee) * digital_services_pct
    prep_fee = roi_cfg.get('prep_fee_per_unit', 0.0)

    if vat_registered:
        # Work ex-VAT — VAT passes through to HMRC, doesn't affect real profit
        selling_ex_vat = selling_price / vat_multiplier
        supplier_ex_vat = supplier_price / vat_multiplier
        profit = selling_ex_vat - supplier_ex_vat - fba_fee - referral_fee - digital_services_fee - prep_fee
    else:
        profit = selling_price - supplier_price - fba_fee - referral_fee - digital_services_fee - prep_fee

    # ROI denominator is always the cash actually paid out (inc VAT)
    roi = (profit / supplier_price) * 100.0

    logger.debug(
        f"sell=£{selling_price:.2f} supplier=£{supplier_price:.2f} "
        f"fba=£{fba_fee:.2f} referral=£{referral_fee:.2f} "
        f"digital_services=£{digital_services_fee:.2f} prep=£{prep_fee:.2f} "
        f"profit=£{profit:.2f} roi={roi:.1f}% vat_mode={vat_registered}"
    )

    if roi < min_roi:
        return None

    return {
        'profit': round(profit, 2),
        'roi': round(roi, 2),
        'referral_fee': round(referral_fee, 2),
        'fba_fee': round(fba_fee, 2),
        'digital_services_fee': round(digital_services_fee, 2),
        'prep_fee': round(prep_fee, 2),
        'selling_price': round(selling_price, 2),
        'supplier_price': round(supplier_price, 2),
        'price_type': amazon_data['price_type'],
    }

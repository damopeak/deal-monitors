"""ArbiSource API client.

Auth is two-step: the extension API key is exchanged for a JWT which lasts ~32 days.
The field is `extensionToken` — sending `token` returns {"error":"Invalid"} with HTTP 200.

The site sits behind AWS WAF which fingerprints the TLS handshake, so plain `requests`
gets a 403. curl_cffi impersonating Chrome passes.
"""
import logging
import os
import time

from curl_cffi import requests as cr

logger = logging.getLogger(__name__)

BASE = "https://app.arbisource.com/api"
JWT_MAX_AGE = 24 * 60 * 60 * 24  # refresh at 24 days, well inside the ~32 day expiry


class ArbiSourceError(Exception):
    pass


class ArbiSourceClient:
    def __init__(self, api_key=None, region=None, cost_overrides=None):
        self.api_key = api_key or os.environ.get("ARBISOURCE_API_KEY")
        self.region = region or os.environ.get("ARBISOURCE_REGION", "UK")
        # Sent with every price-drop request so ArbiSource works the fees out with the
        # real costs. The account itself has these at 0, which overstates every profit.
        self.cost_overrides = cost_overrides or {}
        if not self.api_key:
            raise ArbiSourceError("ARBISOURCE_API_KEY not set (see .env)")
        self.session = cr.Session(impersonate="chrome")
        self.session.headers.update({"region": self.region})
        self._jwt = None
        self._jwt_at = 0
        self._user = None
        self._stores = None

    # ---------- auth ----------

    def _authenticate(self):
        r = self.session.post(f"{BASE}/user/validateExtensionToken",
                              json={"extensionToken": self.api_key}, timeout=60)
        if r.status_code != 200:
            raise ArbiSourceError(f"auth failed: HTTP {r.status_code} {r.text[:200]}")
        data = r.json()
        if "token" not in data:
            raise ArbiSourceError(f"auth rejected: {data}")
        self._jwt = data["token"]
        self._jwt_at = time.time()
        self.session.headers["Authorization"] = f"Bearer {self._jwt}"
        logger.info("ArbiSource authenticated (region=%s, plan=%s)",
                    data.get("region"), data.get("subscriptionType"))
        return self._jwt

    def jwt(self):
        if not self._jwt or (time.time() - self._jwt_at) > JWT_MAX_AGE:
            self._authenticate()
        return self._jwt

    def _call(self, method, path, **kw):
        self.jwt()
        kw.setdefault("timeout", 120)
        url = f"{BASE}{path}"
        r = self.session.request(method, url, **kw)
        if r.status_code in (401, 403) and "html" not in r.headers.get("content-type", ""):
            logger.warning("%s %s -> %s, re-authenticating", method, path, r.status_code)
            self._authenticate()
            r = self.session.request(method, url, **kw)
        if r.status_code != 200:
            raise ArbiSourceError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    # ---------- account ----------

    def user_data(self, refresh=False):
        if self._user is None or refresh:
            self._user = self._call("GET", "/user/getUserData")
        return self._user

    def user_id(self):
        return str(self.user_data()["_id"])

    def store_names(self, refresh=False):
        """getAllStores returns a dict keyed by store name."""
        if self._stores is None or refresh:
            self._stores = list(self._call("GET", "/store/getAllStores").keys())
        return self._stores

    # ---------- price drops ----------

    # ArbiSource applies the account's saved filters server-side before returning
    # anything. Those are looser than our own gates but still throttle the stream to
    # roughly a third. Sending a permissive set hands us the raw feed and lets
    # arbisource_filter do the judging.
    WIDE_FILTERS = {
        "profitBoundaries": [0, 9999], "averageProfitBoundaries": [0, 9999],
        "ROIBoundaries": [0, 9999], "salesRankNumberBoundaries": [0, 9999999],
        "salesRankFilterType": "number", "costBoundaries": [0, 9999],
        "sellerBoundaries": [0, 999], "discountBoundaries": [0, 100],
        "confidenceBoundaries": [0, 100], "imageConfidenceBoundaries": [0, 100],
        "stringConfidenceBoundaries": [0, 100], "dealScoreBoundaries": [0, 10],
        "salesEstimateBoundaries": [0, 999999], "salesConfirmedBoundaries": [0, 999999],
        "salesFilterType": "confirmed", "variationCountBoundaries": [1, 999],
        "amazonRatingBoundaries": [0, 5], "salesRankBoundaries": [0, 100],
        "averageProfitDays": 90, "useAverageProfit": False,
        "excludeHazmat": False, "excludeMismatches": False, "mustHaveBuyBox": False,
        "mustHaveSalesRank": False, "excludeOversized": False, "mustHaveFees": False,
        "excludeAmazonOnListing": False, "amazonMustShareBuyBox": False,
        "excludeIPIssues": False, "excludeOutOfStock": False, "excludeRestricted": False,
        "eanMatchesOnly": False, "excludedStores": [],
    }

    def wide_filters(self):
        """Account filters with every numeric boundary opened up."""
        fs = dict(self.user_data().get("filterSettings") or {})
        fs.update(self.WIDE_FILTERS)
        return fs

    def get_scan_results(self, scan_id, skip=0, per_page=80, filter_settings=None,
                         sort_field="Deal Score", sort_order="desc"):
        """Results of a completed scan. Same product shape as a price drop."""
        u = self.user_data()
        body = {
            "sortConfig": [{"field": sort_field, "order": sort_order}],
            "alreadyRendered": [],
            "filterSettings": filter_settings if filter_settings is not None else u["filterSettings"],
            "showResultsType": ["store", "reverse", "category", "wholesale", "custom"],
            "scanID": scan_id,
            "resultsPerPage": per_page,
            "skip": skip,
            "VATStatus": u.get("VATStatus", "Registered"),
            "stores": [],
        }
        r = self._call("POST", "/result/getScanResults", json=body)
        return r.get("results", r if isinstance(r, list) else [])

    def scans(self, only_completed=True):
        s = self._call("GET", "/scan/getAllScans")
        return [x for x in s if x.get("completedDateTime")] if only_completed else s

    def count_price_drops(self, drop_type="matched"):
        return self._call("GET", f"/pricedrops/countPriceDrops?type={drop_type}").get("totalDrops", 0)

    def get_price_drops(self, drop_type="matched", page=0, per_page=80,
                        sort_field="Most Recent", sort_order="desc",
                        filter_settings=None, use_average_price=False,
                        average_profit_days=90):
        """Fetch a page of price drops.

        Mirrors the web app's own request. `filterSettings` defaults to the account's
        saved global filters — the same ones the Price Drops page uses.
        """
        u = self.user_data()
        body = {
            "sortConfig": [{"field": sort_field, "order": sort_order}],
            "dropsPerPage": per_page,
            "page": page,
            "type": drop_type,
            "hiddenStores": [],
            "selectedStores": self.store_names(),
            "filterSettings": filter_settings if filter_settings is not None else u["filterSettings"],
            "fixedCost": self.cost_overrides.get("fixed_cost", u.get("fixedCost", 0)),
            "inboundShippingCost": self.cost_overrides.get(
                "inbound_shipping_cost", u.get("inboundShippingCost", 0)),
            "storageMonths": self.cost_overrides.get("storage_months", u.get("storageMonths", 0)),
            "VATStatus": u.get("VATStatus", "Registered"),
            "useAveragePrice": use_average_price,
            "averageProfitDays": average_profit_days,
            "hiddenStoreCategories": [],
        }
        return self._call("POST", "/pricedrops/getPriceDrops", json=body).get("drops", [])

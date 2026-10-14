"""Continuous ArbiSource price-drop monitor -> Discord.

ArbiSource pushes price drops over socket.io, but the event carries only a count —
the product data has to be fetched over REST. So this runs as:

    socket event  ->  debounce  ->  fetch newest drops  ->  filter  ->  Discord

Their own Discord integration does not cover price drops (it covers A2A flips only),
which is why this exists.

Run:
    python arbisource_monitor.py              # live monitor
    python arbisource_monitor.py --backfill   # score the current feed and exit
    python arbisource_monitor.py --dry-run    # evaluate but post nothing
"""
import argparse
import json
import logging
import os
import signal
import sys
import threading
import time

import requests
import socketio
import yaml
from dotenv import load_dotenv

from arbisource_client import ArbiSourceClient
from config_loader import read_yaml
import arbisource_filter as af
from arbisource_filter import _num

load_dotenv()

# Windows consoles default to cp1252, which cannot encode £ or emoji — without this
# a single such character in a log line takes the monitor down.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.FileHandler("arbisource_monitor.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("arbisource")
logging.getLogger("socketio").setLevel(logging.WARNING)
logging.getLogger("engineio").setLevel(logging.WARNING)

SOCKET_URL = "https://app.arbisource.com"
# The websocket handshake goes through the same AWS WAF as the REST API and is
# rejected with a 403 without browser-shaped headers.
SOCKET_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
    "Origin": "https://app.arbisource.com",
}
SEEN_FILE = "arbisource_seen.json"
SEEN_MAX = 5000
RESUBSCRIBE_SECONDS = 300      # the web app re-emits every 5 minutes
DEBOUNCE_SECONDS = 8           # drops arrive in bursts; batch them
MIN_FETCH_INTERVAL = 60        # never hit the REST API more often than this
SAFETY_POLL_SECONDS = 600      # fetch anyway if the socket has gone quiet
MAX_POSTS_PER_BATCH = 8        # a burst should never flood the channel

COLOUR = {"elite": 0xFFD700, "strong": 0x00AA00}

# Image fields come back as CDN-relative paths (e.g. "product_images/amazon_B0.jpg").
# Discord rejects the whole embed if handed a relative URL.
CDN_BASE = "https://d3r1ok86eo9ahu.cloudfront.net"


def absolute_image(path):
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return f"{CDN_BASE}/{path.lstrip('/')}"


# --------------------------------------------------------------------------- config

def load_config():
    cfg = read_yaml("config.yaml")  # config.yaml + secrets from .env
    a = cfg.get("arbisource") or {}
    a.setdefault("webhook_url", cfg.get("discord_webhook_url"))
    a.setdefault("watch_unmatched", False)
    a.setdefault("vat_registered", True)
    a.setdefault("ping_on_elite", True)
    a.setdefault("announce_backlog_on_start", False)
    a.setdefault("filter", {})
    a.setdefault("costs", {})
    a.setdefault("wide_feed", True)
    return a


# --------------------------------------------------------------------------- state

class Seen:
    """Remembers drop ids so a re-fired drop doesn't double-ping."""

    def __init__(self, path=SEEN_FILE):
        self.path = path
        self.ids = []
        self._set = set()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.ids = json.load(fh)
                self._set = set(self.ids)
            except (json.JSONDecodeError, OSError):
                logger.warning("could not read %s, starting fresh", path)

    def __contains__(self, key):
        return key in self._set

    def add(self, key):
        if key in self._set:
            return
        self._set.add(key)
        self.ids.append(key)
        if len(self.ids) > SEEN_MAX:
            drop = self.ids[:-SEEN_MAX]
            self.ids = self.ids[-SEEN_MAX:]
            self._set.difference_update(drop)

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.ids, fh)
        os.replace(tmp, self.path)


def drop_key(d):
    return str(d.get("_id") or f"{d.get('ASIN')}|{d.get('storeName')}|{d.get('storeDiscountedPrice')}")


# --------------------------------------------------------------------------- discord

def build_embed(drop, verdict):
    m = verdict["metrics"]
    asin = drop.get("ASIN", "")
    title = (drop.get("storeProductName") or drop.get("amazonProductName") or "Unknown")[:240]
    is_elite = verdict["tier"] == "elite"

    def pct(x):
        return f"{x:+.0f}%" if x else "n/a"

    spike = m["spike_ratio"]
    price_note = "n/a"
    if spike is not None:
        delta = (spike - 1) * 100
        price_note = f"£{m['current_sell']:.2f} ({delta:+.0f}% vs 90d £{m['avg90']:.2f})"

    buy = f"£{m['cash_price']:.2f} — {drop.get('storeName', '?')}"
    if m["discounted"]:
        buy += f"\n~~£{m['list_price']:.2f}~~ ({drop.get('reduction', 0):.0f}% off)"

    fields = [
        {"name": "Buy", "value": buy, "inline": True},
        {"name": "Sell (now)", "value": price_note, "inline": True},
        {"name": "Score", "value": f"**{verdict['score']:.0f}**/100", "inline": True},

        {"name": "ROI @90d avg", "value": f"**{m['roi_90']:.0f}%**", "inline": True},
        {"name": "Profit @90d avg", "value": f"**£{m['profit_90']:.2f}**", "inline": True},
        {"name": "ROI today", "value": f"{m['roi_now']:.0f}% (£{m['profit_now']:.2f})", "inline": True},

        {"name": "Velocity", "value": f"{m['rank_drops_30']} rank drops/30d"
                                      + (f" ÷{m['variations']} var" if m["variations"] > 1 else ""),
         "inline": True},
        {"name": "BSR", "value": f"{m['bsr']:,} ({m['bsr_pct']:.2f}%)", "inline": True},
        {"name": "Sellers", "value": f"{m['sellers']} ({m['fba_offers']} FBA)", "inline": True},
    ]

    # Match evidence — ArbiSource's EAN flag is not proof (a Ninja pressure cooker came
    # back EAN-matched to a pizza oven), so show what the match actually rests on.
    if m["ean_match"]:
        match_note = f"EAN {m['store_ean'] or 'match'}"
    else:
        match_note = f"no EAN, confidence {m['confidence']:.2f}"
    if m["model_codes"]:
        match_note += f" • model {'/'.join(m['model_codes'])}"
    match_note += f" • similarity {m['similarity']:.2f}"
    fields.append({"name": "Match", "value": match_note, "inline": False})
    if m["amazon_name"]:
        fields.append({"name": "Amazon listing", "value": str(m["amazon_name"])[:300],
                       "inline": False})

    links = []
    if drop.get("storeUrl"):
        links.append(f"[Supplier]({drop['storeUrl']})")
    if asin:
        links.append(f"[Amazon](https://www.amazon.co.uk/dp/{asin})")
        links.append(f"[Keepa](https://keepa.com/#!product/2-{asin})")
    if links:
        fields.append({"name": "Links", "value": " • ".join(links), "inline": False})

    footer = f"ASIN {asin} • {drop.get('amazonCategory', '?')}"
    if m["keepa_age_days"] is not None:
        footer += f" • Keepa {m['keepa_age_days']}d old"
    if drop.get("currentPromoCode"):
        footer += f" • code {drop['currentPromoCode']}"

    # Out-of-stock and IP-flagged drops are allowed through, so say so loudly —
    # the numbers above are meaningless if you cannot actually buy the item today.
    warnings = []
    if not m["in_stock"]:
        warnings.append(f"⚠️ **{drop.get('stockStatus') or 'Out of stock'}** at supplier")
    if m["ip_status"] not in ("green", "unknown"):
        warnings.append(f"⚠️ **IP status {m['ip_status']}** — check before buying")
    if drop.get("hazmat"):
        warnings.append("⚠️ **dangerous goods** — needs DG approval for FBA")
    if drop.get("oversized"):
        warnings.append("⚠️ oversized")
    # A near-total collapse is more often a bad scrape than a deal, and the ROI looks
    # spectacular either way. Show the observed price trail so it can be judged at a glance.
    if verdict.get("suspect_price"):
        trail = " → ".join(f"£{_num(p.get('price')):.2f}"
                           for p in (drop.get("priceHistory") or [])[-3:])
        warnings.append(f"⚠️ **{m['reduction']:.0f}% off — likely a bad scrape, check the site**"
                        + (f"\n   observed: {trail}" if trail else ""))

    embed = {
        "title": ("🔥 " if is_elite else "✅ ") + title,
        "color": 0xFF8800 if warnings else COLOUR.get(verdict["tier"], 0x888888),
        "fields": fields,
        "footer": {"text": footer[:2048]},
    }
    if warnings:
        embed["description"] = "\n".join(warnings)
    link = drop.get("storeUrl") or (f"https://www.amazon.co.uk/dp/{asin}" if asin else None)
    if link:
        embed["url"] = link
    thumb = absolute_image(drop.get("amazonImageUrl") or drop.get("storeImageUrl"))
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    return embed


def post(webhook, drop, verdict, ping):
    payload = {"embeds": [build_embed(drop, verdict)]}
    if ping and verdict["tier"] == "elite":
        payload["content"] = "@here 🔥 elite price drop"
    for attempt in range(3):
        try:
            r = requests.post(webhook, json=payload, timeout=20)
            if r.status_code == 429:
                wait = r.json().get("retry_after", 2)
                logger.warning("discord rate limited, waiting %.1fs", wait)
                time.sleep(float(wait) + 0.5)
                continue
            if r.status_code >= 400:
                logger.error("discord %s: %s", r.status_code, r.text[:300])
                return False
            return True
        except requests.RequestException as e:
            logger.warning("discord post failed (%s), retry %d", e, attempt + 1)
            time.sleep(2)
    return False


# --------------------------------------------------------------------------- core

class Monitor:
    def __init__(self, cfg, dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.client = ArbiSourceClient(cost_overrides=cfg.get("costs"))
        self.seen = Seen()
        self.user_id = self.client.user_id()
        # Subscribed with the account's own filters (that is what the socket keys on),
        # but fetched with permissive ones so our gates see the full stream.
        self.filters = self.client.user_data()["filterSettings"]
        self.fetch_filters = (self.client.wide_filters() if cfg.get("wide_feed")
                              else self.filters)
        self.pending = threading.Event()
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.stats = {"events": 0, "fetched": 0, "passed": 0, "posted": 0}
        self.sio = socketio.Client(reconnection=True, reconnection_delay=2,
                                   reconnection_delay_max=60, randomization_factor=0.5)
        self._wire()

    # -- socket --

    def _wire(self):
        @self.sio.event
        def connect():
            logger.info("socket connected (sid=%s)", self.sio.sid)
            self._subscribe()

        @self.sio.event
        def disconnect():
            logger.warning("socket disconnected")

        @self.sio.event
        def connect_error(data):
            logger.error("socket connect error: %s", data)

        # Both events are treated purely as "something changed" nudges — neither
        # carries product data, and matchedPriceDrop fires rarely while
        # unmatchedPriceDrop fires constantly. What we always fetch is the
        # *matched* list, so the extra nudges only improve freshness.
        def on_drop(*args):
            self.stats["events"] += 1
            self.pending.set()

        self.sio.on("matchedPriceDrop", on_drop)
        self.sio.on("unmatchedPriceDrop", on_drop)

    def _subscribe(self):
        self.sio.emit("join_room", self.user_id)
        types = ["matched", "unmatched"] if self.cfg["watch_unmatched"] else ["matched"]
        for t in types:
            self.sio.emit("update_price_drop_filters",
                          {"type": t, "action": "add",
                           "userID": self.user_id, "filters": self.filters})
        logger.info("subscribed: %s", ", ".join(types))

    # -- work --

    def process(self, drops, announce=True):
        passed = []
        for d in drops:
            key = drop_key(d)
            if key in self.seen:
                continue
            self.seen.add(key)
            v = af.evaluate(d, self.cfg["filter"], self.cfg["vat_registered"])
            if v["pass"]:
                passed.append((d, v))
            else:
                logger.debug("reject %s: %s", d.get("ASIN"), "; ".join(v["reasons"][:3]))

        self.stats["fetched"] += len(drops)
        self.stats["passed"] += len(passed)
        passed.sort(key=lambda t: t[1]["score"], reverse=True)

        for d, v in passed[:MAX_POSTS_PER_BATCH]:
            logger.info("PASS [%s %.0f] %s | ROI@90d %.0f%% £%.2f | %d drops/30d | £%.2f @ %s",
                        v["tier"], v["score"], (d.get("storeProductName") or "")[:55],
                        v["metrics"]["roi_90"], v["metrics"]["profit_90"],
                        v["metrics"]["rank_drops_30"], v["metrics"]["cost"], d.get("storeName"))
            if announce and not self.dry_run:
                if post(self.cfg["webhook_url"], d, v, self.cfg["ping_on_elite"]):
                    self.stats["posted"] += 1
                time.sleep(1.2)

        if len(passed) > MAX_POSTS_PER_BATCH:
            logger.info("%d further qualifying drops held back this batch",
                        len(passed) - MAX_POSTS_PER_BATCH)
        self.seen.save()
        return passed

    def fetch_and_process(self, pages=1, per_page=80, announce=True):
        with self.lock:
            all_drops = []
            for p in range(pages):
                try:
                    all_drops += self.client.get_price_drops(
                        drop_type="matched", page=p, per_page=per_page,
                        filter_settings=self.fetch_filters)
                except Exception as e:
                    logger.error("fetch failed (page %d): %s", p, e)
                    break
            return self.process(all_drops, announce=announce)

    # -- loops --

    def _worker(self):
        # Priming already fetched, so start the clock now rather than at the epoch —
        # otherwise the safety poll fires immediately on startup.
        last_fetch = time.time()
        while not self.stopping.is_set():
            nudged = self.pending.wait(timeout=5)
            since = time.time() - last_fetch

            if nudged:
                if since < MIN_FETCH_INTERVAL:
                    continue                      # rate-limit ourselves, keep the flag set
                time.sleep(DEBOUNCE_SECONDS)      # let the burst settle
                self.pending.clear()
                reason = f"{self.stats['events']} socket event(s)"
            elif since >= SAFETY_POLL_SECONDS:
                reason = "safety poll (socket quiet)"
            else:
                continue

            self.stats["events"] = 0
            last_fetch = time.time()
            logger.info("fetching — %s", reason)
            try:
                self.fetch_and_process()
            except Exception as e:
                logger.exception("processing failed: %s", e)

    def _keepalive(self):
        while not self.stopping.wait(RESUBSCRIBE_SECONDS):
            if self.sio.connected:
                try:
                    self._subscribe()
                except Exception as e:
                    logger.warning("resubscribe failed: %s", e)

    def run(self):
        logger.info("monitor starting — user %s, %d drops currently matched",
                    self.user_id, self.client.count_price_drops("matched"))

        # Prime the seen set from the existing backlog so the first burst doesn't
        # announce drops that were already sitting there before we started.
        if not self.seen.ids and not self.cfg.get("announce_backlog_on_start"):
            primed = self.fetch_and_process(pages=2, announce=False)
            logger.info("primed %d ids from the existing backlog (%d would have qualified)",
                        len(self.seen.ids), len(primed))

        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()
        self.sio.connect(SOCKET_URL, transports=["websocket"], headers=SOCKET_HEADERS)
        try:
            while not self.stopping.is_set():
                time.sleep(1)
        finally:
            self.stop()

    def stop(self, *_):
        if self.stopping.is_set():
            return
        self.stopping.set()
        try:
            self.sio.disconnect()
        except Exception:
            pass
        self.seen.save()
        logger.info("stopped — %s", self.stats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="score the current feed once and exit")
    ap.add_argument("--pages", type=int, default=3, help="pages to pull when backfilling")
    ap.add_argument("--dry-run", action="store_true", help="evaluate but post nothing")
    args = ap.parse_args()

    cfg = load_config()
    if not cfg.get("webhook_url"):
        sys.exit("No Discord webhook configured (set ARBISOURCE_WEBHOOK_URL or DISCORD_WEBHOOK_URL in .env)")

    mon = Monitor(cfg, dry_run=args.dry_run)

    if args.backfill:
        passed = mon.fetch_and_process(pages=args.pages, announce=not args.dry_run)
        total = mon.stats["fetched"]
        logger.info("backfill: %d/%d passed (%.1f%%)", len(passed), total,
                    100.0 * len(passed) / total if total else 0.0)
        return

    signal.signal(signal.SIGINT, mon.stop)
    signal.signal(signal.SIGTERM, mon.stop)
    mon.run()


if __name__ == "__main__":
    main()

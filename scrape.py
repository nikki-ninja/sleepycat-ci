#!/usr/bin/env python3
"""
Sleepycat competitor watcher.
Runs daily (via GitHub Actions). For each rival it:
  - pulls the public Shopify /products.json (server-side, no CORS) -> prices, SKUs, sale state
  - fetches the homepage HTML -> detects offer % and UX signals (quiz / EMI / trial / reviews / whatsapp / store)
  - diffs against yesterday's snapshot -> writes human-readable changes to data/changelog.json
Non-Shopify sites degrade gracefully to homepage-signal-only.

No secrets, no headless browser. requests only.
The dashboard reads data/changelog.json (raw.githubusercontent URL) and auto-fills the Daily Log.
"""
import json, os, re, time, datetime
import requests

BRANDS = {
    "tsc":        "https://thesleepcompany.in",
    "flo":        "https://www.flomattress.com",
    "wakefit":    "https://www.wakefit.co",
    "duroflex":   "https://www.duroflexworld.com",
    "emma":       "https://www.emma-sleep.in",
    "sleepyhead": "https://mysleepyhead.com",
    "kurlon":     "https://kurlon.com",
    "springtek":  "https://springtek.in",
    "sleepycat":  "https://sleepycat.in",   # <-- your own site, so you self-monitor too
}

# homepage keyword -> signal name (fuzzy presence detection)
SIGNALS = {
    "quiz":     [r"find your\s+(perfect\s+)?mattress", r"sleep quiz", r"take the quiz", r"mattress finder"],
    "emi":      [r"no[-\s]?cost emi", r"easy emi", r"emi option"],
    "trial":    [r"100[-\s]?night", r"100[-\s]?day", r"night trial", r"risk[-\s]?free trial"],
    "reviews":  [r"judge\.me", r"yotpo", r"stamped", r"okendo", r"reviews\.io", r"verified review"],
    "whatsapp": [r"wa\.me", r"whatsapp", r"api\.whatsapp"],
    "store":    [r"find a store", r"store locator", r"visit (a )?store", r"experience (centre|center)"],
    "finance":  [r"pay later", r"snapmint", r"simpl", r"bajaj finserv"],
}
OFFER_RE = re.compile(r"(\d{1,2})\s*%\s*(off|discount)", re.I)
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SleepycatWatcher/1.0)"}
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SNAP = os.path.join(DATA_DIR, "competitors.json")
LOG = os.path.join(DATA_DIR, "changelog.json")
TODAY = datetime.date.today().isoformat()


def get(url, **kw):
    return requests.get(url, headers=HEADERS, timeout=25, **kw)


def pull_shopify(base):
    """Return dict of {title: price} + on_sale flag, or None if not Shopify."""
    products, on_sale = {}, False
    page = 1
    while page <= 4:  # be polite: max 4 pages
        try:
            r = get(f"{base}/products.json?limit=250&page={page}")
            if r.status_code != 200 or "application/json" not in r.headers.get("content-type", ""):
                return None if page == 1 else _pack(products, on_sale)
            items = r.json().get("products", [])
        except Exception:
            return None if page == 1 else _pack(products, on_sale)
        if not items:
            break
        for p in items:
            prices = []
            for v in p.get("variants", []):
                try:
                    pr = float(v.get("price") or 0)
                    prices.append(pr)
                    cap = v.get("compare_at_price")
                    if cap and float(cap) > pr:
                        on_sale = True
                except (TypeError, ValueError):
                    pass
            if prices:
                products[p.get("title", "?").strip()] = round(min(prices))
        page += 1
        time.sleep(0.4)
    return _pack(products, on_sale)


def _pack(products, on_sale):
    return {"products": products, "on_sale": on_sale} if products else None


def scan_home(base):
    """Detect offer % and UX signals from homepage HTML."""
    out = {"offer": None, "signals": {}}
    try:
        html = get(base).text.lower()
    except Exception:
        return out
    m = OFFER_RE.search(html)
    if m:
        out["offer"] = int(m.group(1))
    for name, pats in SIGNALS.items():
        out["signals"][name] = any(re.search(p, html) for p in pats)
    return out


def snapshot():
    snap = {"generated": datetime.datetime.utcnow().isoformat() + "Z", "date": TODAY, "brands": {}}
    for key, base in BRANDS.items():
        rec = {"platform": "other", "products": {}, "on_sale": None, "count": 0}
        shop = pull_shopify(base)
        if shop:
            rec["platform"] = "shopify"
            rec["products"] = shop["products"]
            rec["on_sale"] = shop["on_sale"]
            rec["count"] = len(shop["products"])
        home = scan_home(base)
        rec["offer"] = home["offer"]
        rec["signals"] = home["signals"]
        snap["brands"][key] = rec
        print(f"[{key}] platform={rec['platform']} skus={rec['count']} offer={rec['offer']} sale={rec['on_sale']}")
    return snap


def diff(prev, cur):
    """Produce human-readable change entries."""
    entries = []
    if not prev:
        return entries
    for key, cb in cur["brands"].items():
        pb = prev["brands"].get(key, {})
        # new SKUs
        new = [t for t in cb.get("products", {}) if t not in pb.get("products", {})]
        for t in new[:5]:
            entries.append((key, "new-product", f"new SKU live - '{t}' at Rs {cb['products'][t]:,}"))
        # price moves on matched SKUs
        for t, price in cb.get("products", {}).items():
            old = pb.get("products", {}).get(t)
            if old and old != price:
                arrow = "↓" if price < old else "↑"
                entries.append((key, "price", f"'{t}' Rs {old:,} {arrow} Rs {price:,}"))
        # sale flip
        if pb.get("on_sale") is not None and cb.get("on_sale") != pb.get("on_sale"):
            entries.append((key, "sale", "site-wide sale " + ("ON" if cb["on_sale"] else "OFF")))
        # offer % change
        if cb.get("offer") != pb.get("offer") and cb.get("offer") is not None:
            entries.append((key, "offer", f"headline offer now {cb['offer']}% off"))
        # signal changes (added / removed UX modules)
        for s, val in cb.get("signals", {}).items():
            oldv = pb.get("signals", {}).get(s)
            if oldv is not None and val != oldv:
                entries.append((key, "ux", f"{'added' if val else 'removed'} homepage '{s}' signal"))
    return entries


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    prev = json.load(open(SNAP)) if os.path.exists(SNAP) else None
    cur = snapshot()
    changes = diff(prev, cur)

    log = json.load(open(LOG)) if os.path.exists(LOG) else []
    for brand, typ, text in changes:
        log.insert(0, {"date": TODAY, "brand": brand, "type": typ, "text": text})
    log = log[:400]  # cap

    json.dump(cur, open(SNAP, "w"), indent=2, ensure_ascii=False)
    json.dump(log, open(LOG, "w"), indent=2, ensure_ascii=False)
    print(f"\n{len(changes)} change(s) logged for {TODAY}.")


if __name__ == "__main__":
    main()

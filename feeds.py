"""
Structured listing feeds: sites whose web page is an app shell but whose app
reads a public JSON endpoint. These need no AI: each record already has
address, price and beds, so they go straight to the same rules as the
alert-email listings.

Each fetcher returns cards shaped like the email parser's output.
"""

import re
import time

import requests

from posts import BROWSER_UA


def nooklyn(feed):
    """Nooklyn (Brooklyn rental brokerage). Its /rentals page is a JS app that
    POSTs to /api/v3/web/listings.search; prices come back in cents."""
    cards, page, pages = [], 1, 1
    while page <= min(pages, feed.get("max_pages", 15)):
        r = requests.post("https://nooklyn.com/api/v3/web/listings.search",
                          json={"bed_list": [0, 1], "page": page},
                          headers={**BROWSER_UA, "Accept": "application/json"}, timeout=30)
        r.raise_for_status()
        j = r.json()
        pages = j.get("page_count") or 1
        for l in j.get("listings", []):
            if not l.get("rental") or not l.get("residential"):
                continue
            short = l.get("short_address") or ""
            addr, _, unit = short.partition(" - Unit:")
            cards.append({
                "id": f"nooklyn:{l['id']}",
                "address": addr.strip() or (l.get("address") or "").split(",")[0],
                "unit": unit.strip() or None,
                "price": round(l["price"] / 100) if l.get("price") else None,
                "beds": int(l["bedrooms"]) if l.get("bedrooms") is not None else None,
                "is_room": False, "is_sale": False, "is_gone": False,
                "url": "https://nooklyn.com" + l["url"] if l.get("url") else "https://nooklyn.com/rentals",
                "source": "Nooklyn",
            })
        page += 1
        time.sleep(1)
    return cards


def brooklyn_group(feed):
    """Brooklyn Group: a Next.js app that ships every listing in the page's
    embedded __NEXT_DATA__ JSON (about 2,000 cards, one GET)."""
    import json
    r = requests.get("https://www.brooklyngroup.com/apartments", headers=BROWSER_UA, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    data = json.loads(m.group(1))
    items = data["props"]["pageProps"]["initialState"]["properties"]["propertyCards"]
    cards = []
    for l in items:
        title = l.get("title") or ""
        addr, _, unit = title.partition(" #")
        cards.append({
            "id": f"bg:{l['value']}",
            "address": (addr or (l.get("address") or "").split(",")[0]).strip(),
            "unit": unit.strip() or None,
            "price": int(l["price"]) if l.get("price") else None,
            "beds": int(l["bedrooms"]) if l.get("bedrooms") is not None else None,
            "is_room": False, "is_sale": False, "is_gone": False,
            "url": f"https://www.brooklyngroup.com/apartments?id={l['value']}",
            "source": "Brooklyn Group",
        })
    return cards


FETCHERS = {"nooklyn": nooklyn, "brooklyn_group": brooklyn_group}


def check_feeds(cfg, state, log):
    """Return new cards from every enabled feed. The first time a feed is
    seen, everything already on it is recorded silently."""
    feeds_state = state.setdefault("feeds", {})
    now, out = time.time(), []
    for feed in cfg.get("feeds", []):
        if feed.get("enabled") is False:
            continue
        fs = feeds_state.setdefault(feed["name"], {"seen": {}, "checked": 0})
        if now - fs["checked"] < feed.get("every_minutes", 15) * 60 - 30:
            continue
        try:
            cards = FETCHERS[feed["type"]](feed)
        except (requests.RequestException, ValueError, KeyError) as e:
            log(f"  feed {feed['name']}: failed ({e.__class__.__name__}: {str(e)[:80]})")
            continue
        first = not fs["seen"]
        new = [c for c in cards if c["id"] not in fs["seen"]]
        log(f"  feed {feed['name']}: {len(cards)} listings, {len(new)} new"
            + (" (first visit: recorded silently)" if first else ""))
        if not first:
            out += new
        for c in cards:
            if now - fs["seen"].get(c["id"], 0) > 86400:
                fs["seen"][c["id"]] = now
        fs["seen"] = {k: v for k, v in fs["seen"].items() if now - v < 45 * 86400}
        fs["checked"] = now
    return out

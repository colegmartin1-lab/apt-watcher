#!/usr/bin/env python3
"""
apt_watcher.py — NYC apartment listing watcher
Polls configured sources, dedupes against a local SQLite DB,
and pushes new listings to your phone via ntfy.sh.

Usage:
    python3 apt_watcher.py            # run one poll cycle
    python3 apt_watcher.py --test     # send a test notification
    python3 apt_watcher.py --list     # show everything seen so far
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "seen_listings.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

# A persistent session keeps cookies between requests, which some sites
# require before serving content.
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------- storage ---

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS listings (
               url        TEXT PRIMARY KEY,
               title      TEXT,
               price      TEXT,
               source     TEXT,
               first_seen TEXT
           )"""
    )
    conn.commit()
    return conn


def is_new(conn, url):
    cur = conn.execute("SELECT 1 FROM listings WHERE url = ?", (url,))
    return cur.fetchone() is None


def record(conn, listing):
    conn.execute(
        "INSERT OR IGNORE INTO listings (url, title, price, source, first_seen) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            listing["url"],
            listing.get("title", ""),
            listing.get("price", ""),
            listing.get("source", ""),
            datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------- filters ---

def parse_price(text):
    """Extract a numeric price from strings like '$5,883' -> 5883."""
    if not text:
        return None
    m = re.search(r"\$?\s*([\d,]+)", text)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def passes_filters(listing, filters):
    price = parse_price(listing.get("price", ""))
    if price is not None:
        if filters.get("max_price") and price > filters["max_price"]:
            return False
        if filters.get("min_price") and price < filters["min_price"]:
            return False

    title = (listing.get("title") or "").lower()
    for kw in filters.get("exclude_keywords", []):
        if kw.lower() in title:
            return False

    required = filters.get("require_any_keywords", [])
    if required and not any(kw.lower() in title for kw in required):
        return False

    return True


# --------------------------------------------------------------- adapters ---

class Blocked(Exception):
    """Site actively refused us (bot detection)."""


def fetch(url, timeout=20):
    resp = SESSION.get(url, timeout=timeout)
    if resp.status_code in (403, 429):
        raise Blocked(
            f"HTTP {resp.status_code} — this site blocks automated requests. "
            f"Use its native saved-search alerts instead."
        )
    resp.raise_for_status()
    return resp.text


def scrape_craigslist(source):
    """
    Parse a Craigslist search results page.
    Handles both the static no-JS fallback (li.cl-static-search-result)
    and the older gallery markup (li.cl-search-result / .result-row).
    """
    html = fetch(source["url"])
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    # Modern static fallback served to non-JS clients
    for li in soup.select("li.cl-static-search-result"):
        a = li.find("a", href=True)
        if not a:
            continue
        title_el = li.select_one(".title")
        price_el = li.select_one(".price")
        loc_el = li.select_one(".location")
        listings.append(
            {
                "url": a["href"].split("#")[0],
                "title": title_el.get_text(strip=True) if title_el else a.get_text(strip=True),
                "price": price_el.get_text(strip=True) if price_el else "",
                "location": loc_el.get_text(strip=True) if loc_el else "",
                "source": source["name"],
            }
        )

    # Older markup fallbacks
    if not listings:
        for row in soup.select("li.cl-search-result, li.result-row"):
            a = row.select_one("a.posting-title, a.result-title, a[href]")
            if not a or not a.get("href"):
                continue
            price_el = row.select_one(".priceinfo, .result-price, .price")
            listings.append(
                {
                    "url": urljoin(source["url"], a["href"]).split("#")[0],
                    "title": a.get_text(strip=True),
                    "price": price_el.get_text(strip=True) if price_el else "",
                    "source": source["name"],
                }
            )

    return listings


PRICE_RE = re.compile(r"\$\s*[\d,]{4,}")


def scrape_generic(source):
    """
    Generic adapter: watch any page for new links whose URL matches
    `link_pattern` (regex). Works for RentHop search pages, Leasebreak,
    Listings Project archives, management-company 'available units'
    pages, etc. Tries to pull a price from the listing card surrounding
    each link so price filters can apply.
    """
    html = fetch(source["url"])
    soup = BeautifulSoup(html, "html.parser")
    pattern = re.compile(source["link_pattern"])
    seen_urls = set()
    listings = []

    for a in soup.find_all("a", href=True):
        full = urljoin(source["url"], a["href"]).split("#")[0].split("?")[0]
        if not pattern.search(full) or full in seen_urls:
            continue
        seen_urls.add(full)
        text = a.get_text(strip=True)

        # Look for a price in the link text, then walk up a few ancestor
        # elements (the "listing card") until one is found.
        price = ""
        m = PRICE_RE.search(text)
        node = a
        for _ in range(4):
            if m:
                break
            node = node.parent
            if node is None:
                break
            m = PRICE_RE.search(node.get_text(" ", strip=True)[:600])
        if m:
            price = m.group(0).replace(" ", "")

        listings.append(
            {
                "url": full,
                "title": text[:120] if text else full,
                "price": price,
                "source": source["name"],
            }
        )

    return listings


ADAPTERS = {
    "craigslist": scrape_craigslist,
    "generic": scrape_generic,
}


# ---------------------------------------------------------- notifications ---

def notify_desktop(listing):
    """Native macOS banner via osascript (no-op on other platforms)."""
    if sys.platform != "darwin":
        return
    import subprocess
    title = listing.get("title", "New listing").replace('"', "'")[:80]
    sub = " · ".join(
        p for p in (listing.get("price", ""), listing.get("source", "")) if p
    ).replace('"', "'")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{sub}" with title "🏠 {title}" sound name "Glass"'],
            timeout=5, capture_output=True,
        )
    except Exception:
        pass


def notify(config, listing):
    """Push to phone via ntfy.sh (free, no signup: subscribe to your topic
    in the ntfy app), plus a native desktop banner when running on a Mac.
    Falls back to stdout if no topic configured."""
    notify_desktop(listing)
    # NTFY_TOPIC env var wins so cloud runners can keep the topic in a secret
    topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic")
    title = f"New: {listing.get('title', 'listing')}"
    price = listing.get("price", "")
    loc = listing.get("location", "")
    body_parts = [p for p in (price, loc, listing.get("source", "")) if p]
    body = " · ".join(body_parts)

    line = f"[NOTIFY] {title} | {body} | {listing['url']}"
    print(line)

    if not topic:
        return

    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=listing["url"].encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Tags": "house",
                "Click": listing["url"],
                "Priority": "high",
                "Message": body.encode("utf-8") if body else b"New listing",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        print(f"[WARN] notification failed: {exc}", file=sys.stderr)


# -------------------------------------------------------------------- run ---

def load_config():
    if not CONFIG_PATH.exists():
        print(f"Missing config: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def run_cycle(config, conn, quiet_first_run=True):
    filters = config.get("filters", {})
    total_new = 0
    first_run = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0

    for source in config.get("sources", []):
        if source.get("enabled") is False:
            continue
        adapter = ADAPTERS.get(source.get("type"))
        if not adapter:
            print(f"[WARN] unknown source type: {source.get('type')}", file=sys.stderr)
            continue
        try:
            listings = adapter(source)
        except Blocked as exc:
            print(f"[BLOCKED] {source['name']}: {exc}", file=sys.stderr)
            continue
        except Exception as exc:
            print(f"[WARN] {source['name']}: {exc}", file=sys.stderr)
            continue

        fresh = [
            l for l in listings
            if is_new(conn, l["url"]) and passes_filters(l, filters)
        ]

        for listing in fresh:
            record(conn, listing)
            total_new += 1
            # On the very first run, seed the DB silently so you don't get
            # blasted with 100 notifications for existing listings.
            if not (first_run and quiet_first_run):
                notify(config, listing)
                time.sleep(1)  # be gentle with ntfy

        print(
            f"[{datetime.now():%H:%M:%S}] {source['name']}: "
            f"{len(listings)} found, {len(fresh)} new"
        )

    if first_run and quiet_first_run and total_new:
        print(f"[INFO] First run: seeded {total_new} existing listings silently. "
              f"You'll be notified about anything new from now on.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="send test notification")
    parser.add_argument("--list", action="store_true", help="print seen listings")
    args = parser.parse_args()

    config = load_config()
    conn = init_db()

    if args.test:
        notify(config, {
            "url": "https://example.com",
            "title": "Test notification — apt_watcher is alive",
            "price": "$0",
            "source": "test",
        })
        return

    if args.list:
        for row in conn.execute(
            "SELECT first_seen, source, price, title, url FROM listings "
            "ORDER BY first_seen DESC"
        ):
            print(" | ".join(str(c) for c in row))
        return

    run_cycle(config, conn)


if __name__ == "__main__":
    main()

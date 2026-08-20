#!/usr/bin/env python3
"""
apt_watcher.py — NYC apartment listing watcher
Polls configured sources, dedupes against a local SQLite DB,
and pushes new listings to your phone via ntfy.sh.

Usage:
    python3 apt_watcher.py            # run one poll cycle
    python3 apt_watcher.py --test     # send a test notification
    python3 apt_watcher.py --list     # show everything seen so far

One script drives several independent trackers. Each gets its own config +
DB (and usually its own ntfy topic) so their notifications never mix:
    python3 apt_watcher.py --config config.sublets.json --db seen_sublets.db
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
# Defaults for the original (long-term rentals) tracker. Both are overridable
# with --config/--db so additional trackers can run from this same script
# without their DBs or notifications bleeding into each other.
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


def fetch_listing_date(url, timeout=15):
    """Posting datetime from a Craigslist detail page, or None if unavailable.
    The search-results page carries no dates, so we read the listing page
    (only ever done once per listing, when it's first seen)."""
    try:
        html = SESSION.get(url, timeout=timeout).text
        t = BeautifulSoup(html, "html.parser").select_one("time[datetime]")
        if not t or not t.get("datetime"):
            return None
        dt = datetime.fromisoformat(t["datetime"])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def is_stale(url, max_age_days):
    """True only when we can prove the posting is older than max_age_days.
    Unknown age -> False (never suppress on uncertainty)."""
    if not max_age_days or "craigslist.org" not in url:
        return False
    dt = fetch_listing_date(url)
    if dt is None:
        return False
    return (datetime.now(timezone.utc) - dt).days > max_age_days


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

    # Some rejects are shapes rather than phrases -- "9 Days:", "Oct 1-15",
    # "2 wks" all mean "shorter than a month" and there are too many spellings
    # to list as substrings.
    for pat in filters.get("exclude_regex", []):
        if re.search(pat, title, re.I):
            return False

    # Some sources (Reddit, Listings Project) carry the neighborhood in the
    # title/slug rather than a structured field, so they require an explicit
    # neighborhood match instead of relying on a geo search radius.
    required_loc = filters.get("require_any_locations", [])
    if required_loc:
        hay = f"{title} {(listing.get('location') or '').lower()}"
        if not any(kw.lower() in hay for kw in required_loc):
            return False

    # Neighborhood backstop: circles can't cleanly exclude Crown Heights /
    # Bed-Stuy without also dropping Clinton Hill / Prospect Heights, so we
    # also drop by the listing's location tag. Only fires on an affirmative
    # tag match; untagged listings still pass (the circle already vetted them).
    location = (listing.get("location") or "").lower()
    if location:
        for bad in filters.get("exclude_locations", []):
            if bad.lower() in location:
                return False

    required = filters.get("require_any_keywords", [])
    if required and not any(kw.lower() in title for kw in required):
        return False

    return True


def is_preferred(listing, filters):
    """True when the listing explicitly advertises something we're after
    (e.g. 'month to month'). Not a filter -- requiring the phrase would drop
    most real listings, since plenty of good ones just never say it. It only
    decides whether the push gets a highlight marker."""
    prefer = filters.get("prefer_keywords", [])
    if not prefer:
        return False
    hay = f"{(listing.get('title') or '')} {(listing.get('detail') or '')}".lower()
    return any(kw.lower() in hay for kw in prefer)


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


UUID_SUFFIX = re.compile(
    r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Listings Project pages quote several rates at once (per night / per week /
# per month), so grabbing the first dollar figure reads a nightly rate as if
# it were rent. Always pull the monthly figure specifically.
MONTHLY_RE = re.compile(r"\$\s*([\d,]{3,7})\s*(?:/|per\s+)\s*month", re.I)
SHORT_TERM_RE = re.compile(
    r"(\$\s*[\d,]+\s*(?:/|per\s+)\s*(?:night|week))|nightly|minimum stay", re.I
)


def scrape_listingsproject(source):
    """
    Listings Project (weekly NYC newsletter, listingsproject.com).

    Worth having because it's the one working source that covers Manhattan —
    Craigslist's Manhattan geo-search is broken, so West Village / SoHo can
    only come from here. Cards are image-only links, but the URL slug carries
    the neighborhood and bedroom count, e.g.
        /listings/west-village-1-bedroom-apartment-<uuid>
    Price only exists on the detail page, so we fetch it once per listing.
    """
    html = fetch(source["url"])
    soup = BeautifulSoup(html, "html.parser")
    listings, seen = [], set()

    for a in soup.find_all("a", href=True):
        if "/listings/" not in a["href"]:
            continue
        full = urljoin(source["url"], a["href"]).split("?")[0]
        if full in seen:
            continue
        seen.add(full)

        slug = UUID_SUFFIX.sub("", full.split("/listings/")[-1])
        title = slug.replace("-", " ").strip()

        price, terms = "", ""
        try:
            detail = SESSION.get(full, timeout=15)
            if detail.status_code == 200:
                text = BeautifulSoup(detail.text, "html.parser").get_text(" ", strip=True)
                m = MONTHLY_RE.search(text)
                if m:
                    price = "$" + m.group(1)
                # Nightly/weekly rates mean this is a furnished short-stay, not
                # a lease. Tag it so config can filter it out by location.
                if SHORT_TERM_RE.search(text):
                    terms = " short-term-rental"
            time.sleep(0.3)  # be a polite guest
        except requests.RequestException:
            pass  # price stays unknown; filters treat that as "don't reject"

        listings.append(
            {
                "url": full,
                "title": title[:140],
                "price": price,
                # slug names the neighborhood; terms flag short-stay rentals
                "location": slug.replace("-", " ") + terms,
                "source": source["name"],
            }
        )

    return listings


SUBLETCOM_PROP_RE = re.compile(r"sublet\.com/property/\d+")
SUBLETCOM_HOOD_RE = re.compile(r"[Rr]ental listing in ([^.]+?)\.")


def scrape_sublet_com(source):
    """
    Sublet.com -- one of the few short-term sites that doesn't 403 us.

    Its cards are the best-structured data of any source here: each one spells
    out the unit type ("Apartment" vs "Room Rental") and the term ("Month to
    Month" vs "Min 6 Months"), which are exactly the two things that decide
    whether a sublet is worth seeing. Both land in the title so the ordinary
    keyword filters can act on them.

    The catch is that the listing page is borough-wide with no neighborhood on
    the card, so we fetch each detail page once for its "Rental listing in
    <neighborhood>, Brooklyn." line and filter on that. The page is heavy and
    slow, hence the longer timeout.
    """
    html = fetch(source["url"], timeout=source.get("timeout", 45))
    soup = BeautifulSoup(html, "html.parser")
    listings, seen = [], set()

    for a in soup.find_all("a", href=True):
        if not SUBLETCOM_PROP_RE.search(a["href"]):
            continue
        full = a["href"].split("?")[0].replace("http://", "https://")
        if full in seen:
            continue
        seen.add(full)

        # The link itself is an image; the useful text lives on the card, so
        # walk up until an ancestor shows a price.
        card, node = "", a
        for _ in range(5):
            node = node.parent
            if node is None:
                break
            card = node.get_text(" ", strip=True)[:220]
            if PRICE_RE.search(card):
                break
        card = re.sub(r"\s*(View Listing|Group Message|Contact|Phone|Send Message)\s*", " ", card).strip()

        m = PRICE_RE.search(card)
        location = ""
        try:
            detail = SESSION.get(full, timeout=20)
            if detail.status_code == 200:
                text = BeautifulSoup(detail.text, "html.parser").get_text(" ", strip=True)
                hood = SUBLETCOM_HOOD_RE.search(text)
                if hood:
                    location = hood.group(1).strip()
            time.sleep(0.3)  # be a polite guest
        except requests.RequestException:
            pass  # unknown location; require_any_locations will drop it

        listings.append(
            {
                "url": full,
                "title": card[:140],
                "price": m.group(0).replace(" ", "") if m else "",
                "location": location,
                "source": source["name"],
            }
        )

    return listings


REDDIT_ENTRY_RE = re.compile(r"<entry>(.*?)</entry>", re.S)
REDDIT_FIELD_RE = {
    "title": re.compile(r"<title>(.*?)</title>", re.S),
    "link": re.compile(r'<link[^>]*href="([^"]+)"'),
}


def scrape_reddit(source):
    """
    Reddit via public RSS (no auth, no API key). High-noise: most posts are
    'ISO' requests, rants, and news rather than listings, so config supplies
    require_any_keywords to keep only posts that look like a real offer in a
    neighborhood we care about.

    Reddit rate-limits hard (429) — one feed per cycle, and Blocked/errors
    are swallowed upstream so a throttle never breaks the run.
    """
    xml = fetch(source["url"])
    listings = []

    for chunk in REDDIT_ENTRY_RE.findall(xml):
        t = REDDIT_FIELD_RE["title"].search(chunk)
        l = REDDIT_FIELD_RE["link"].search(chunk)
        if not t or not l:
            continue
        title = re.sub(r"&amp;", "&", t.group(1)).strip()
        m = PRICE_RE.search(title)
        listings.append(
            {
                "url": l.group(1).split("?")[0],
                "title": title[:140],
                "price": m.group(0).replace(" ", "") if m else "",
                "location": title,  # neighborhood, when named, is in the title
                "source": source["name"],
            }
        )

    return listings


ADAPTERS = {
    "craigslist": scrape_craigslist,
    "generic": scrape_generic,
    "listingsproject": scrape_listingsproject,
    "reddit": scrape_reddit,
    "sublet_com": scrape_sublet_com,
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


def notify(config, listing, preferred=False):
    """Push to phone via ntfy.sh (free, no signup: subscribe to your topic
    in the ntfy app), plus a native desktop banner when running on a Mac.
    Falls back to stdout if no topic configured."""
    notify_desktop(listing)
    # NTFY_TOPIC env var wins so cloud runners can keep the topic in a secret
    topic = os.environ.get("NTFY_TOPIC") or config.get("ntfy_topic")
    prefix = config.get("notify_prefix", "New")
    # A listing that actually says "month to month" is worth spotting in a
    # crowded feed, so it gets a marker rather than a separate channel.
    mark = "\u2b50 " if preferred else ""
    title = f"{mark}{prefix}: {listing.get('title', 'listing')}"
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
                "Tags": "star,house" if preferred else "house",
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


def run_cycle(config, conn, quiet_first_run=True, seed_only=False):
    filters = config.get("filters", {})
    total_new = 0
    first_run = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0

    for source in config.get("sources", []):
        if source.get("enabled") is False:
            continue
        if "type" not in source:
            continue  # comment/marker entry (e.g. {"_scenario": ...})
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

        # A source may tighten (or loosen) the global filters for itself.
        src_filters = {**filters, **source.get("filters", {})}
        fresh = [
            l for l in listings
            if is_new(conn, l["url"]) and passes_filters(l, src_filters)
        ]

        max_age = config.get("notify_max_age_days")
        stale_ct = 0
        for listing in fresh:
            record(conn, listing)
            total_new += 1
            # Stay silent when (a) seeding on purpose (--seed, e.g. after adding
            # new sources) or (b) the very first run on an empty DB — otherwise
            # you'd get blasted with notifications for pre-existing listings.
            if seed_only:
                continue
            if first_run and quiet_first_run:
                continue
            # Suppress blatantly stale posts, but keep them recorded so we don't
            # re-check their date every cycle. Unknown age still notifies.
            if is_stale(listing["url"], max_age):
                stale_ct += 1
                continue
            notify(config, listing, preferred=is_preferred(listing, src_filters))
            time.sleep(1)  # be gentle with ntfy

        stale_note = f", {stale_ct} stale-skipped" if stale_ct else ""
        print(
            f"[{datetime.now():%H:%M:%S}] {source['name']}: "
            f"{len(listings)} found, {len(fresh)} {'seeded' if seed_only else 'new'}{stale_note}"
        )

    if seed_only and total_new:
        print(f"[INFO] Seeded {total_new} current listings silently. "
              f"Only listings newer than now will notify.")
    elif first_run and quiet_first_run and total_new:
        print(f"[INFO] First run: seeded {total_new} existing listings silently. "
              f"You'll be notified about anything new from now on.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="send test notification")
    parser.add_argument("--list", action="store_true", help="print seen listings")
    parser.add_argument("--seed", action="store_true",
                        help="record current listings as seen WITHOUT notifying "
                             "(run once after adding/changing sources)")
    parser.add_argument("--config", metavar="PATH",
                        help="config file to run (default: config.json)")
    parser.add_argument("--db", metavar="PATH",
                        help="seen-listings DB for this tracker "
                             "(default: seen_listings.db). Give each tracker "
                             "its own, or they'll dedupe against each other.")
    args = parser.parse_args()

    # Each tracker is just a (config, db) pair, so pointing these elsewhere is
    # all it takes to run a second, fully independent watcher.
    global CONFIG_PATH, DB_PATH
    if args.config:
        CONFIG_PATH = Path(args.config).expanduser()
        if not CONFIG_PATH.is_absolute():
            CONFIG_PATH = BASE_DIR / CONFIG_PATH
    if args.db:
        DB_PATH = Path(args.db).expanduser()
        if not DB_PATH.is_absolute():
            DB_PATH = BASE_DIR / DB_PATH

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

    if args.seed:
        run_cycle(config, conn, seed_only=True)
        return

    run_cycle(config, conn)


if __name__ == "__main__":
    main()

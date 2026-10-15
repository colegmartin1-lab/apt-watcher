"""
Broker / property-manager websites ("pages" in config.inbox.json).

Every site is laid out differently, so instead of a parser per site this
watches each listings page for NEW links. Each link is taken together with
the text around it (its card), and only cards for links we haven't seen
before go to Claude Haiku, which pulls out address, price, size and lease
type. Unchanged pages cost nothing; the first visit to a page just records
what's already there, silently.
"""

import hashlib
import re
import time
from typing import List, Literal, Optional
from urllib.parse import urljoin, urldefrag

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from posts import BROWSER_UA

LISTINGISH = re.compile(r"\$\s?\d|\bstudio\b|\bbed|\bbr\b|bedroom|/mo\b|per month|apartment|\bapt\b|"
                        r"\brent(al|als)?\b|\bfor rent\b", re.I)
PRICEISH = re.compile(r"\$\s?\d")
PRICES = re.compile(r"\$\s?[\d,]+")
SKIP_HREF = re.compile(r"^(mailto:|tel:|javascript:|#)|\.(jpg|jpeg|png|gif|pdf)$|"
                       r"facebook\.com|instagram\.com|twitter\.com|x\.com/|linkedin\.com|"
                       r"tiktok\.com|youtube\.com|google\.com/maps", re.I)
MAX_CARD_CHARS = 500
MAX_CARDS_PER_CALL = 25


def _h(s):
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def link_cards(html, base_url):
    """{absolute href: text of the smallest enclosing block that reads like a
    listing card}. Climbs from each link to its parents until the block has
    some substance, so price/beds/address sitting next to the link come along."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        t.decompose()
    cards = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or SKIP_HREF.search(href):
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        node = a
        text = re.sub(r"\s+", " ", node.get_text(" ")).strip()
        for _ in range(6):
            # Stop once the block carries a price (the card is complete)...
            if PRICEISH.search(text) and len(text) >= 40:
                break
            parent = node.parent
            if parent is None or parent.name in ("body", "html", "[document]"):
                break
            # ...or before it swallows a menu / the whole grid: a card links
            # to one listing a few times at most.
            if len({x["href"] for x in parent.find_all("a", href=True)}) > 5:
                break
            ptext = re.sub(r"\s+", " ", parent.get_text(" ")).strip()
            if len(ptext) > 900:
                break
            node, text = parent, ptext
        if not LISTINGISH.search(text):
            continue
        text = text[:MAX_CARD_CHARS]
        prev = cards.get(url)
        if (prev and text not in prev and prev not in text and PRICEISH.search(text)
                and set(PRICES.findall(text)) != set(PRICES.findall(prev))):
            # Different priced cards behind one generic link (some sites link
            # every card to the same page): track each card separately.
            cards[f"{url}#card-{_h(text)}"] = text
        elif prev is None or len(text) > len(prev):
            # Same listing linked several times (photo, title, "details"):
            # keep the richest context.
            cards[url] = text
    return cards


JUNK_PRICE_LINE = re.compile(r"under \$|\bfrom \$\d+ to|application fee|deposit|\$\s?\d+\s*(/|per)\s*(night|week)",
                             re.I)


def price_text(html):
    """Single-building pages (RentCafe etc.) list units as plain text, not
    links. Return just the priced lines with a little context, for change
    detection and for the AI to read."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        t.decompose()
    lines = [re.sub(r"\s+", " ", l).strip() for l in soup.get_text("\n").splitlines()]
    lines = [l for l in lines if l]
    keep = set()
    for i, l in enumerate(lines):
        if PRICEISH.search(l) and not JUNK_PRICE_LINE.search(l):
            keep.update(range(max(0, i - 3), min(len(lines), i + 3)))
    title = re.sub(r"\s+", " ", soup.title.get_text()).strip() if soup.title else ""
    body = " | ".join(lines[i] for i in sorted(keep))[:4000]
    return f"Page: {title} | {body}" if body else ""


class PageListing(BaseModel):
    card: int = Field(description="Number of the card this listing came from")
    is_offer: bool = Field(description="True only for an apartment currently offered for rent. "
                           "False for sold/sales listings, agent bios, blog posts, neighborhood guides, "
                           "'rented'/'leased' units, or navigation links.")
    kind: Literal["whole_apartment", "room_in_shared", "other"]
    price_monthly: Optional[int] = Field(description="Monthly rent in USD, or null")
    bedrooms: Optional[int] = Field(description="0 for studio, else bedroom count; null if unknown")
    neighborhood: Optional[str]
    address: Optional[str] = Field(description="Street address, e.g. '123 Clinton Ave'; null if not shown")
    lease: Literal["lease", "lease_takeover", "long_sublet", "short_term", "unknown"]
    available: Optional[str]
    summary: str = Field(description="One line, max 80 chars")


class PageReading(BaseModel):
    listings: List[PageListing]


PAGE_PROMPT = """These are numbered cards scraped from a NYC real-estate firm's \
or landlord's website. Return one entry per rental apartment listing; skip cards \
that aren't listings at all. A card may hold several listings (e.g. a building \
page with floor plans): return one entry per bedroom type, all with that card's \
number. On a landlord's building page, a floor plan with a rent or rent range \
counts as an offer: use the lowest rent shown for that bedroom type. Be literal: \
only report what the card says or clearly implies (a URL slug like \
'/rentals/123-clinton-ave-1br' or the page title counts)."""


def read_cards(reader, firm, cards):
    """One Haiku call for up to MAX_CARDS_PER_CALL new cards from one page."""
    if reader.budget <= 0:
        return None
    reader.budget -= 1
    items = list(cards.items())[:MAX_CARDS_PER_CALL]
    content = f"Firm: {firm}\n\n" + "\n\n".join(
        f"[{i}] {text}\nlink: {url}" for i, (url, text) in enumerate(items))
    resp = reader.call(PAGE_PROMPT, content, PageReading, max_tokens=8000)
    if resp is None:
        return None
    out = []
    for item in resp.listings:
        if 0 <= item.card < len(items):
            url, text = items[item.card]
            url = re.split(r"#(card|text)-", url)[0]
            out.append(({"url": url, "source": firm, "title": text[:140]}, item))
    return out


def read_email_cards(reader, name, cards, min_price, max_price):
    """Listing cards from an alert email/newsletter → (post, reading) pairs.
    Cards whose every price is outside the budget are skipped before the AI
    sees them (a weekly digest can hold hundreds of listings)."""
    def in_budget(text):
        prices = [int(p.replace(",", "").replace("$", "").strip()) for p in PRICES.findall(text)
                  if p.replace(",", "").replace("$", "").strip().isdigit()]
        return not prices or any(min_price <= x <= max_price for x in prices)
    seen_text, keep = set(), {}
    for url, text in cards.items():
        if text not in seen_text and in_budget(text):  # same listing linked twice
            seen_text.add(text)
            keep[url] = text
    out, items = [], list(keep.items())
    for i in range(0, len(items), MAX_CARDS_PER_CALL):
        got = read_cards(reader, name, dict(items[i:i + MAX_CARDS_PER_CALL]))
        if got is None:
            return out if out else None
        out += got
    return out


def check_pages(cfg, state, reader, log, verbose=False):
    """Yield (post, reading) for new listings on configured firm pages."""
    pages_state = state.setdefault("pages", {})
    now = time.time()
    results = []
    for page in cfg.get("pages", []):
        if page.get("enabled") is False:
            continue
        url, name = page["url"], page["name"]
        ps = pages_state.setdefault(_h(url), {"seen": {}, "checked": 0})
        if now - ps["checked"] < page.get("every_minutes", 15) * 60 - 30:
            continue
        try:
            r = requests.get(url, headers=BROWSER_UA, timeout=20)
            r.raise_for_status()
        except requests.RequestException as e:
            log(f"  page {name}: fetch failed ({e.__class__.__name__})")
            continue
        if page.get("mode") == "text":
            # One building/listing per page: watch its priced text instead of links.
            snippet = price_text(r.text)
            fp = _h(re.sub(r"\d{1,2}/\d{1,2}(/\d{2,4})?", "", snippet))  # ignore dates
            cards = {f"{url}#text-{fp}": snippet} if snippet else {}
        else:
            cards = link_cards(r.text, url)
        if page.get("skip_cards_matching"):
            # e.g. a "Random properties" sidebar that reshuffles every load
            skip = re.compile(page["skip_cards_matching"], re.I)
            cards = {u: t for u, t in cards.items() if not skip.search(t)}
        new = {u: t for u, t in cards.items() if _h(u) not in ps["seen"]}
        first_visit = not ps["seen"] and ps["checked"] == 0
        if new and not first_visit:
            got = read_cards(reader, name, new)
            if got is None:
                log(f"  page {name}: {len(new)} new links, AI unavailable — will retry")
                continue  # leave them unseen and unchecked so the next run retries
            results += got
        log(f"  page {name}: {len(cards)} listing-ish links, {len(new)} new"
            + (" (first visit: recorded silently)" if first_visit else ""))
        for u in cards:
            # Refresh at most daily so state doesn't churn every run.
            if now - ps["seen"].get(_h(u), 0) > 86400:
                ps["seen"][_h(u)] = now
        ps["seen"] = {k: v for k, v in ps["seen"].items() if now - v < 45 * 86400}
        ps["checked"] = now
    return results

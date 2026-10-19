#!/usr/bin/env python3
"""
Inbox watcher: turns the listing-alert emails you already get (StreetEasy,
Zillow, Trulia, RentHop, Leasebreak, ...) into a short, strict feed on your
phone.

Every run it:
  1. reads recent alert emails from Gmail over IMAP (app password),
  2. pulls each listing out (address, unit, price, beds, link),
  3. looks the address up in NYC's free GeoSearch to get the real
     neighborhood + coordinates (so it doesn't trust whatever the email says),
  4. drops anything that isn't a studio/1BR rental in budget in an allowed
     neighborhood (rooms, for-sale homes, 2BR+, wrong area...),
  5. merges duplicates (same apartment on Zillow + Trulia + StreetEasy),
  6. pushes each survivor to ntfy once.

Usage:
  python inbox_watcher.py              # normal run
  python inbox_watcher.py --dry-run    # print decisions, notify nothing, save nothing
  python inbox_watcher.py --seed       # mark everything current as handled, silently
  python inbox_watcher.py --dump DIR   # save the raw alert emails (for parser work)
  python inbox_watcher.py --file X.eml # parse saved emails instead of Gmail

Secrets come from env vars (GMAIL_APP_PASSWORD, NTFY_TOPIC) or, locally,
from ~/.apt-watcher.env (KEY=value lines).
"""

import argparse
import email
import hashlib
import html
import imaplib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "apt-watcher/2.0 (personal apartment alert filter)"}


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def load_env_file():
    p = Path.home() / ".apt-watcher.env"
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state(path):
    if path.exists():
        return json.loads(path.read_text())
    return {"processed": {}, "notified": {}}


def save_state(path, state):
    # Keep the file small: forget emails/listings older than 30 days.
    cutoff = time.time() - 30 * 86400
    state["processed"] = {k: v for k, v in state["processed"].items() if v > cutoff}
    state["notified"] = {k: v for k, v in state["notified"].items() if v["ts"] > cutoff}
    state["posts_seen"] = {k: v for k, v in state.get("posts_seen", {}).items() if v > cutoff}
    path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# Gmail
# --------------------------------------------------------------------------

def fetch_emails(cfg, lookback_days):
    """Yield (gmail_id, email.message.Message) for alert emails in the window."""
    user = cfg["gmail_user"]
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not pw:
        raise RuntimeError("GMAIL_APP_PASSWORD is not set")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(user, pw)
    # All Mail, so alerts still count if a Gmail filter archives/labels them.
    imap.select('"[Gmail]/All Mail"', readonly=True)
    senders = " OR ".join(cfg["alert_senders"])
    query = f"from:({senders}) newer_than:{lookback_days}d"
    typ, data = imap.search(None, "X-GM-RAW", f'"{query}"')
    if typ != "OK":
        raise RuntimeError(f"IMAP search failed: {data}")
    for num in data[0].split():
        typ, msgdata = imap.fetch(num, "(X-GM-MSGID BODY.PEEK[])")
        if typ != "OK" or not msgdata or not isinstance(msgdata[0], tuple):
            continue
        meta = msgdata[0][0].decode()
        m = re.search(r"X-GM-MSGID (\d+)", meta)
        gid = m.group(1) if m else hashlib.sha1(msgdata[0][1]).hexdigest()
        yield gid, email.message_from_bytes(msgdata[0][1])
    imap.logout()


def message_parts(msg):
    """Return (sender_address, subject, html_body, text_body)."""
    sender = parseaddr(msg.get("From", ""))[1].lower()
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    html_body = text_body = ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        body = payload.decode(charset, errors="replace")
        if ctype == "text/html" and not html_body:
            html_body = body
        elif ctype == "text/plain" and not text_body:
            text_body = body
    return sender, subject, html_body, text_body


# --------------------------------------------------------------------------
# parsing helpers
# --------------------------------------------------------------------------

PRICE_RE = re.compile(r"\$\s?(\d{1,2},?\d{3})(?!\s*[kKmM]\b)")
SALE_RE = re.compile(r"\$\s?\d+(?:\.\d+)?\s*[KkMm]\b|\bfor sale\b", re.I)
BEDS_RE = re.compile(r"\b(studio|(\d)\s*(?:bd|bds|bed|beds|bedrooms?|br)\b)", re.I)
# A room in someone else's apartment, not "LARGE LIVING ROOM" in amenities.
ROOM_RE = re.compile(r"sqft room\b|\broom for rent\b|\bprivate room\b|"
                     r"\bshared (?:apartment|apt|room)\b|\broommates?\b", re.I)
GONE_RE = re.compile(r"\b(RENTED|OFF MARKET|NO LONGER AVAILABLE|IN CONTRACT|DELISTED)\b")
# Longest spellings first so "Avenue #2" isn't cut off at "Ave".
STREET_WORDS = (r"Street|Avenue|Place|Boulevard|Road|Terrace|Court|Drive|Lane|"
                r"Parkway|Square|Plaza|St|Ave|Pl|Blvd|Rd|Ter|Ct|Dr|Ln|Pkwy|Sq|Plz|"
                r"Way|Walk|Row|Slip|Loop|Broadway|Bowery")
ADDRESS_RE = re.compile(
    rf"(\d{{1,5}}(?:-\d{{1,4}})?\s+(?:[NSEW]\.?\s+|North\s+|South\s+|East\s+|West\s+)?"
    rf"[A-Za-z0-9][A-Za-z0-9.' ]{{0,30}}?\s(?:{STREET_WORDS})\b\.?(?:\s+(?:E|W|N|S|East|West)\b)?)"
    rf"(?:\s*,?\s*(?:#|Apt\.?|APT|Unit|Ste)\s*([A-Za-z0-9-]+))?",
)
INVISIBLE = dict.fromkeys(map(ord, "​‌‍­͏﻿⁠"), None)


def clean_text(s):
    s = html.unescape(s).translate(INVISIBLE).replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def unwrap_url(url):
    """Click-tracking links (Zillow/Trulia) carry the real URL in ?target=."""
    try:
        q = parse_qs(urlparse(url).query)
        for key in ("target", "url", "u", "redirect"):
            if key in q and q[key][0].startswith("http"):
                return unwrap_url(unquote(q[key][0]))
    except ValueError:
        pass
    return url


def html_tokens(html_body):
    """Visible text pieces in reading order, each with the link it sits in."""
    soup = BeautifulSoup(html_body, "html.parser")
    for t in soup(["style", "script", "head", "title"]):
        t.decompose()
    out = []
    for node in soup.find_all(string=True):
        text = clean_text(str(node))
        if text:
            a = node.find_parent("a", href=True)
            out.append((text, unwrap_url(a["href"]) if a else None))
    return out


# --------------------------------------------------------------------------
# alert-email parser
# --------------------------------------------------------------------------

def sender_rule(cfg, sender, subject):
    """Which configured alert type this email is, or None to ignore it.
    Only saved-search alerts are listed in config, so recommendation emails
    ("Homes for You", "rentals you might like", newsletters) never get read."""
    for rule in cfg["email_sources"]:
        if re.search(rule["from"], sender) and re.search(rule.get("subject", ""), subject):
            return rule
    return None


def parse_cards(rule, tokens):
    """Split an alert email into listing cards and pull the facts out of each.

    Each site's emails repeat one block per listing that starts with a
    recognizable line ("For rent", "Rental Unit in Greenpoint", "$2,945/mo").
    Everything after a stop marker ("Other rentals you might like", footer)
    is dropped, which is also what cuts off the recommended extras."""
    start = re.compile(rule["card_start"])
    stops = [m.lower() for m in rule.get("stop_at", [])]
    cards, cur = [], None
    for text, href in tokens:
        if any(text.lower().startswith(m) for m in stops):
            break
        if start.search(text):
            cur = {"tokens": []}
            cards.append(cur)
        if cur is not None:
            cur["tokens"].append((text, href))
    out = []
    for card in cards:
        texts = [t for t, _ in card["tokens"]]
        joined = " ".join(texts)
        addr = unit = None
        addr_ctx = ""
        for i, t in enumerate(texts):
            m = ADDRESS_RE.match(t)
            if m and len(t) < 90:
                addr, unit = clean_text(m.group(1)), m.group(2)
                # The city usually sits on the address line or the next one
                # ("373 Manhattan Ave #3," / "Brooklyn, NY").
                addr_ctx = " ".join(texts[i:i + 2])
                break
        if not addr:
            continue
        price = PRICE_RE.search(joined)
        beds = BEDS_RE.search(joined)
        # Trulia shows studios as just "1ba" with no bedroom count.
        if not beds and rule.get("no_beds_means_studio") and re.search(r"\d\s*ba\b", joined):
            beds = BEDS_RE.search("studio")
        hood_hint = re.search(r"\bin ([A-Z][\w .'-]+)$", texts[0])
        links = [h for _, h in card["tokens"] if h]
        good = [h for h in links if re.search(rule.get("link", "$^"), h)]
        out.append({
            "address": addr, "unit": unit,
            "price": int(price.group(1).replace(",", "")) if price else None,
            "beds": None if not beds else (0 if beds.group(1).lower() == "studio" else int(beds.group(2))),
            "is_room": bool(ROOM_RE.search(joined)),
            "is_sale": bool(SALE_RE.search(joined)),
            "is_gone": bool(GONE_RE.search(joined)),
            "hood_hint": hood_hint.group(1) if hood_hint else None,
            # Only the address line and the site's own neighborhood label say
            # where the apartment is; the card also names the broker's office
            # ("…, New York, NY 10018"), which must not count.
            "borough": borough_of(f"{addr_ctx} {hood_hint.group(1) if hood_hint else ''}", None),
            "url": (good or links or [None])[0],
            "source": rule["name"],
        })
    return out


def parse_email(cfg, sender, subject, html_body, text_body):
    rule = sender_rule(cfg, sender, subject)
    if not rule or not html_body:
        return None  # not a saved-search alert we read
    return parse_cards(rule, html_tokens(html_body))


# --------------------------------------------------------------------------
# geocoding + rules
# --------------------------------------------------------------------------

_geo_cache = {}


MANHATTAN_HINT = re.compile(r"\bnew york,? ny\b|\bmanhattan\b|\b100\d\d\b|west village|\bsoho\b|"
                            r"greenwich village|east village|lower east side|chelsea|tribeca", re.I)
BROOKLYN_HINT = re.compile(r"\bbrooklyn\b|\b112\d\d\b", re.I)


def borough_of(text, default="Brooklyn"):
    """Which borough an address/listing text points at, for the lookup."""
    if BROOKLYN_HINT.search(text or ""):
        return "Brooklyn"
    if MANHATTAN_HINT.search(text or ""):
        return "Manhattan"
    return default


STREET_TYPES = {"street": "st", "avenue": "ave", "place": "pl", "road": "rd", "boulevard": "blvd",
                "terrace": "ter", "court": "ct", "drive": "dr", "lane": "ln", "parkway": "pkwy",
                "north": "n", "south": "s", "east": "e", "west": "w"}


def _street_key(s):
    """'448 Bergen Street' / '448 BERGEN ST' → ('448', 'bergen st')."""
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    words = [STREET_TYPES.get(w, w) for w in s.split()]
    num = words[0] if words and words[0][0].isdigit() else ""
    rest = words[1:] if num else words
    rest = [re.sub(r"(\d+)(st|nd|rd|th)$", r"\1", w) for w in rest]  # 8th → 8
    return num, " ".join(rest)


def geocode(address, borough_hint="Brooklyn", explicit=False):
    """Address → real neighborhood + coordinates via NYC GeoSearch.

    GeoSearch ignores the borough in the query text, and Brooklyn and
    Manhattan share street names (Clinton St, Henry St, Bank St...). So take
    the top few matches and score them: when the listing itself names the
    borough, that wins; otherwise an exact number+street match wins."""
    # Only the city/ZIP part names a borough ("602 Manhattan Ave" is Greenpoint).
    detected = borough_of(address.partition(",")[2], None)
    if detected:
        borough_hint, explicit = detected, True
    key = f"{address.lower()}|{borough_hint}|{explicit}"
    if key in _geo_cache:
        return _geo_cache[key]
    street = address.split(",")[0]
    num, name = _street_key(street)
    result = None
    try:
        r = requests.get("https://geosearch.planninglabs.nyc/v2/search",
                         params={"text": f"{street}, {borough_hint}, NY", "size": 5},
                         headers=UA, timeout=15)
        feats = r.json().get("features") or []

        def score(f):
            p = f["properties"]
            fnum, fname = _street_key(p.get("name") or p.get("label") or "")
            same_street = bool(name) and fname == name
            exact = same_street and num == fnum
            same_borough = p.get("borough") == borough_hint
            if explicit:
                return same_borough * 10 + exact * 3 + same_street
            return exact * 3 + same_borough * 2 + same_street
        if feats:
            best = max(feats, key=score)  # max() keeps GeoSearch's order on ties
            p = best["properties"]
            lon, lat = best["geometry"]["coordinates"]
            result = {"hood": p.get("neighbourhood") or "", "borough": p.get("borough") or "",
                      "label": p.get("name") or p.get("label") or address,
                      "lat": lat, "lon": lon, "confidence": p.get("confidence", 0)}
    except (requests.RequestException, ValueError, KeyError) as e:
        log(f"  geocode failed for {address!r}: {e}")
    _geo_cache[key] = result
    return result


def bqe_lon_at(lat, line):
    """Longitude of the BQE at a given latitude (linear between waypoints)."""
    pts = sorted(line)
    if lat <= pts[0][0]:
        return pts[0][1]
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        if la1 <= lat <= la2:
            return lo1 + (lo2 - lo1) * (lat - la1) / (la2 - la1)
    return pts[-1][1]


def judge_place(cfg, geo):
    """Neighborhood check on a geocoded address. None = OK, else reason."""
    r = cfg["rules"]
    if not geo:
        return "address not found"
    hood = geo["hood"]
    if hood not in r["neighborhoods"]:
        return f"hood {hood or '?'}"
    special = r.get("williamsburg")
    if hood == "Williamsburg" and special:
        if geo["lat"] < special["min_lat"]:
            return "Williamsburg south of Domino"
        if geo["lon"] > bqe_lon_at(geo["lat"], special["bqe_line"]):
            return "Williamsburg east of BQE"
    return None


def judge_size_price(cfg, price, beds, price_required=True):
    r = cfg["rules"]
    if beds is not None and beds > r["max_beds"]:
        return f"{beds} beds"
    if price is None:
        return "no price" if price_required else None
    if not (r["min_price"] <= price <= r["max_price"]):
        return f"price ${price:,}"
    return None


def judge(cfg, c, geo):
    """Alert-email listing. None if it passes, else a short reason."""
    if c["is_gone"]:
        return "rented/off market"
    if c["is_sale"]:
        return "for sale"
    if c["is_room"]:
        return "room/share"
    return judge_size_price(cfg, c["price"], c["beds"]) or judge_place(cfg, geo)


def hood_from_text(cfg, text):
    """Free-text neighborhood ('Ft Greene', 'N. Williamsburg') → allowed name,
    or None. Blanks out 'never' names first so 'East Williamsburg' and
    'South Slope' don't sneak in via 'Williamsburg' / 'Slope', while
    'Gowanus/Park Slope' still counts as Park Slope."""
    t = " " + re.sub(r"[^a-z ]", " ", (text or "").lower()) + " "
    t = re.sub(r"\s+", " ", t)
    for bad in cfg["rules"].get("never_neighborhoods", []):
        t = t.replace(f" {bad.lower()} ", " | ")
    for name, aliases in cfg["rules"]["neighborhood_aliases"].items():
        for a in [name] + aliases:
            if f" {a.lower()} " in t:
                return name
    return None


def norm_unit(u):
    return re.sub(r"[^a-z0-9]", "", (u or "").lower())


def listing_key(geo, address, unit):
    base = (geo["label"] if geo else address).lower()
    return hashlib.sha1(f"{base}|{norm_unit(unit)}".encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# notify
# --------------------------------------------------------------------------

def ntfy(topic, title, body, click=None, tags="house", priority="default", actions=None):
    headers = {"Title": title.encode("utf-8"), "Tags": tags, "Priority": priority}
    if click:
        headers["Click"] = click
    if actions:
        headers["Actions"] = actions
    requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                  headers=headers, timeout=15)


def beds_label(b):
    return "Beds ?" if b is None else ("Studio" if b == 0 else f"{b}BR")


def notify_hit(topic, hit, dry_run, drafted=False):
    price = f"${hit['price']:,}" if hit["price"] else "Price ?"
    title = f"{price} · {beds_label(hit['beds'])} · {hit['hood']}"
    lines = [hit["line"], f"via {', '.join(sorted(hit['sources']))}"]
    actions = None
    if hit.get("takeover"):
        title = "🔁 Takeover lead · " + title
        if hit.get("why"):
            lines.insert(1, f"Why: {hit['why']}")
        if drafted:
            lines.append("✉️ Message drafted in Gmail: review & send")
            actions = (f"view, Open listing, {hit['url']}; "
                       "view, Gmail drafts, https://mail.google.com/mail/u/0/#drafts")
    lines += [f"⚠️ {f}" for f in hit["flags"]]
    body = "\n".join(lines)
    # People's posts get snapped up fastest, so they always ring.
    prio = "high" if hit["is_post"] or (hit["price"] or 0) <= 2500 else "default"
    log(f"  ✅ NOTIFY {title} | {' | '.join(lines)} | {hit['url']}")
    if not dry_run and topic:
        ntfy(topic, title, body, click=hit["url"], priority=prio, actions=actions,
             tags="repeat" if hit.get("takeover") else "speech_balloon" if hit["is_post"] else "house")
        time.sleep(1)


# --------------------------------------------------------------------------
# free-text posts (Reddit, Listings Project, Facebook group emails)
# --------------------------------------------------------------------------

ODDS = {"low": 0, "medium": 1, "high": 2}


def reverse_geocode(lat, lon):
    """Map pin → neighborhood, via NYC GeoSearch's reverse lookup."""
    try:
        r = requests.get("https://geosearch.planninglabs.nyc/v2/reverse",
                         params={"point.lat": lat, "point.lon": lon, "size": 1},
                         headers=UA, timeout=15)
        f = (r.json().get("features") or [None])[0]
        if f:
            p = f["properties"]
            return {"hood": p.get("neighbourhood") or "", "borough": p.get("borough") or "",
                    "label": p.get("name") or p.get("label") or "", "lat": lat, "lon": lon,
                    "confidence": 1.0}
    except (requests.RequestException, ValueError, KeyError) as e:
        log(f"  reverse geocode failed for {lat},{lon}: {e}")
    return None


def judge_post(cfg, post, reading):
    """AI reading of a post → (hit dict, None) or (None, reason)."""
    if reading is None:
        return None, "not read"
    if not reading.is_offer:
        return None, "not an offer"
    if reading.kind != "whole_apartment":
        return None, reading.kind
    if reading.scam_risk == "high":
        return None, "likely scam: " + "; ".join(reading.scam_flags[:2])
    # Leases and lease takeovers are the main search. A sublet only counts as
    # a takeover lead: the tenant may be leaving for good, worth asking.
    takeover = reading.lease in ("short_term", "long_sublet") or \
        (post.get("sublet_context") and reading.lease == "unknown")
    if takeover:
        t = cfg.get("takeover", {})
        if not t.get("enabled"):
            return None, reading.lease.replace("_", " ")
        if reading.tenant_returning:
            return None, "sublet: tenant returning"
        if ODDS[reading.takeover_odds] < ODDS[t.get("min_odds", "medium")]:
            return None, f"sublet, takeover odds {reading.takeover_odds}"
        if reading.lister_type == "host_or_operator":
            return None, "furnished-rental operator"
        if ODDS[reading.scam_risk] > ODDS[t.get("max_scam", "medium")]:
            return None, f"scam risk {reading.scam_risk}"
    # People often leave the price out ("DM for details"); still worth a ping.
    why = judge_size_price(cfg, reading.price_monthly, reading.bedrooms, price_required=False)
    if why:
        return None, why
    flags, geo = [], post.get("geo")  # Craigslist posts carry their own map pin
    if geo:
        pass
    elif reading.address:
        hint = borough_of(f"{reading.neighborhood or ''} {post['title']}", None)
        geo = geocode(reading.address, hint or cfg.get("default_borough", "Brooklyn"),
                      explicit=bool(hint))
    if geo and geo.get("confidence", 0) >= 0.8:
        why = judge_place(cfg, geo)
        if why:
            return None, why
        hood = geo["hood"]
    else:
        hood = hood_from_text(cfg, reading.neighborhood) or \
            hood_from_text(cfg, post["title"])
        if not hood:
            return None, f"hood {reading.neighborhood or '?'}"
        if hood == "Williamsburg":
            flags.append("Williamsburg: check it's N of Domino / W of BQE")
        else:
            flags.append("no address, neighborhood as stated")
    if reading.price_monthly is None:
        flags.append("no price in post")
    if reading.lease == "lease_takeover":
        flags.append("lease takeover")
    if reading.scam_risk == "medium":
        flags.append("possible scam: " + "; ".join(reading.scam_flags[:2]))
    key = listing_key(geo, reading.address, None) if geo and reading.address else \
        hashlib.sha1(post["url"].encode()).hexdigest()[:16]
    line = reading.summary + (f" · avail {reading.available}" if reading.available else "")
    return {"key": key, "price": reading.price_monthly, "beds": reading.bedrooms,
            "hood": hood, "line": line, "url": post["url"], "sources": {post["source"]},
            "flags": flags, "is_post": True, "takeover": takeover,
            "why": "; ".join(reading.takeover_reasons[:2]), "title": post["title"],
            "address": reading.address, "available": reading.available,
            "contact_email": reading.contact_email}, None


def collect_posts(cfg, state, fb_posts, verbose, ai_emails=(), from_files=False):
    """Fetch new posts from every free-text source and run them past the AI."""
    from posts import Reader, fetch_listingsproject, fetch_reddit
    ft = cfg.get("free_text", {})
    if not ft.get("enabled"):
        return [], None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log("free-text sources skipped: ANTHROPIC_API_KEY not set")
        return [], None
    seen = state.setdefault("posts_seen", {})
    posts = list(fb_posts)
    lp_urls = ft.get("listingsproject_urls") or [ft["listingsproject_url"]]
    fetchers = [("Reddit", lambda: fetch_reddit(ft))]
    fetchers += [(f"Listings Project {u.rsplit('/', 1)[-1]}",
                  lambda u=u: fetch_listingsproject({**ft, "listingsproject_url": u}, seen))
                 for u in lp_urls]
    if cfg.get("takeover", {}).get("enabled") and cfg.get("craigslist_sublets"):
        from posts import fetch_craigslist

        def place_check(lat, lon):
            geo = reverse_geocode(lat, lon)
            return geo, judge_place(cfg, geo)

        def craigslist():
            got, skipped = fetch_craigslist(cfg["craigslist_sublets"], seen, place_check,
                                            cfg["rules"]["min_price"], cfg["rules"]["max_price"], log)
            for pid in skipped:
                seen[pid] = time.time()
            return got
        fetchers.append(("Craigslist sublets", craigslist))
    for name, fetcher in fetchers:
        try:
            got = fetcher()
            log(f"{name}: {len(got)} posts")
            posts += got
        except requests.RequestException as e:
            log(f"{name} failed: {e}")  # Reddit 429s now and then; next run retries
    new = [p for p in posts if p["id"] not in seen]
    log(f"{len(new)} new posts to read")
    reader = Reader(ft, log)
    hits = []
    for p in new:
        if reader.budget <= 0:
            break  # out of budget this run: the rest wait for the next run
        reading = reader.read(p)
        if reading is None:
            continue  # API hiccup: leave it unseen so the next run retries
        seen[p["id"]] = time.time()
        hit, why = judge_post(cfg, p, reading)
        if hit:
            hits.append(hit)
        elif verbose:
            log(f"   ✗ [{p['source']}] {p['title'][:70]} — {why}")

    # Emails read by the AI (Leasebreak's weekly list / search alerts).
    from pages import read_email_cards
    r = cfg["rules"]
    for gid, name, cards in ai_emails:
        reader.budget = max(reader.budget, ft.get("ai_max_email_calls", 12))
        got = read_email_cards(reader, name, cards, r["min_price"], r["max_price"])
        if got is None:
            continue  # AI unavailable: email stays unprocessed and is retried next run
        if not from_files:
            state["processed"][gid] = time.time()
        log(f"{name} email: {len(cards)} links, {len(got)} listings read")
        for p, reading in got:
            hit, why = judge_post(cfg, p, reading)
            if hit:
                hits.append(hit)
            elif verbose:
                log(f"   ✗ [{name}] {reading.summary[:70]} — {why}")

    # Broker / property-manager websites: only new links get read.
    from pages import check_pages
    if cfg.get("pages") and not reader.broken:
        reader.budget = ft.get("ai_max_page_calls_per_run", 15)
        for p, reading in check_pages(cfg, state, reader, log, verbose):
            hit, why = judge_post(cfg, p, reading)
            if hit:
                hits.append(hit)
            elif verbose:
                log(f"   ✗ [{p['source']}] {reading.summary[:70]} — {why}")
    return hits, reader.broken


def send_reminders(cfg, state, topic, dry_run):
    """Weekly nudges (e.g. Listings Project First Access needs you signed in,
    so the watcher can't read it; it can remind you to look)."""
    from datetime import timedelta
    now_et = datetime.now(timezone.utc) - timedelta(hours=4)  # EDT; an hour off in winter is fine
    sent = state.setdefault("reminders_sent", {})
    for rem in cfg.get("reminders", []):
        week = now_et.strftime("%G-%V")
        if (now_et.strftime("%A") == rem["weekday"] and now_et.hour >= rem["hour_et"]
                and sent.get(rem["name"]) != week):
            log(f"reminder: {rem['name']}")
            if topic and not dry_run:
                ntfy(topic, rem["title"], rem["body"], click=rem.get("url"), tags="bell")
            if not dry_run:
                sent[rem["name"]] = week


EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")


def outreach_text(cfg, hit):
    """The lease-takeover ask for one lead, from the editable config template."""
    o = cfg["takeover"]["outreach"]
    street = re.sub(r"^\d+[-\d]*\s+", "", hit.get("address") or "").strip()
    where = f"on {street} in {hit['hood']}" if street else f"in {hit['hood']}"
    kind = {None: "apartment", 0: "studio", 1: "1-bedroom"}.get(hit["beds"], f"{hit['beds']}-bedroom")
    source = sorted(hit["sources"])[0]
    fields = {"name": o.get("name", ""), "where": where, "kind": kind, "source": source,
              "title": hit.get("title") or "", "url": hit["url"]}
    return o["subject"].format(**fields), o["body"].format(**fields)


def create_draft(cfg, hit):
    """Save the outreach message to Gmail Drafts (never sends). Addressed only
    when the post itself gave an email; otherwise Cole adds the Craigslist
    relay address or sends it on Facebook."""
    from email.message import EmailMessage
    subject, body = outreach_text(cfg, hit)
    msg = EmailMessage()
    msg["From"] = cfg["gmail_user"]
    to = (hit.get("contact_email") or "").strip()
    if EMAIL_RE.match(to):
        msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(cfg["gmail_user"], os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", ""))
        typ, _ = imap.append('"[Gmail]/Drafts"', "(\\Draft)",
                             imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        imap.logout()
        return typ == "OK"
    except (imaplib.IMAP4.error, OSError) as e:
        log(f"  draft failed: {e}")
        return False


def alert_problem(state, topic, kind, title, body, dry_run):
    """Ping the phone about a broken piece, at most once per 12h per kind,
    so a dead watcher is never silent but never spammy either."""
    last = state.setdefault("problem_alerts", {})
    if topic and not dry_run and time.time() - last.get(kind, 0) > 43200:
        ntfy(topic, title, body, tags="warning", priority="high")
        last[kind] = time.time()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(args):
    load_env_file()
    cfg = json.loads((HERE / args.config).read_text())
    state_path = HERE / args.state
    state = load_state(state_path)
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")

    if args.file:
        msgs = [(Path(f).name, email.message_from_bytes(Path(f).read_bytes())) for f in args.file]
    else:
        try:
            msgs = list(fetch_emails(cfg, args.days))
        except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
            log(f"Gmail failed: {e}")
            alert_problem(state, topic, "gmail", "⚠️ Apartment watcher can't read Gmail",
                          f"{e}\nEmail alerts are paused until this is fixed.", args.dry_run)
            if not args.dry_run:
                save_state(state_path, state)
            return 1

    fresh = [(gid, m) for gid, m in msgs if args.file or gid not in state["processed"]]
    log(f"{len(msgs)} alert emails in last {args.days}d, {len(fresh)} new")

    if args.dump:
        d = Path(args.dump)
        d.mkdir(parents=True, exist_ok=True)
        for gid, m in msgs:
            (d / f"{gid}.eml").write_bytes(m.as_bytes())
        log(f"dumped {len(msgs)} emails to {d}")
        return 0

    # Collect candidates across all sources first, so the same apartment
    # seen on three sites in one run becomes one alert listing all three.
    batch, fb_posts, ai_emails = {}, [], []

    def add(hit):
        if hit["key"] in batch:
            batch[hit["key"]]["sources"] |= hit["sources"]
        else:
            batch[hit["key"]] = hit

    def consider_card(c):
        """A structured listing (alert email or JSON feed) → batch if it passes."""
        geo = geocode(c["address"], c.get("borough") or cfg.get("default_borough", "Brooklyn"),
                      explicit=bool(c.get("borough")))
        why = judge(cfg, c, geo)
        if why:
            if args.verbose:
                log(f"   ✗ [{c['source']}] {c['address']} {c['unit'] or ''} ${c['price']} "
                    f"{beds_label(c['beds'])} [{geo['hood'] if geo else '?'}] — {why}")
            return
        unit = f" #{c['unit']}" if c["unit"] else ""
        add({"key": listing_key(geo, c["address"], c["unit"]), "price": c["price"],
             "beds": c["beds"], "hood": geo["hood"], "line": c["address"] + unit,
             "url": c.get("url"), "sources": {c["source"]}, "flags": [],
             "is_post": False})

    for gid, m in fresh:
        sender, subject, html_body, text_body = message_parts(m)
        ai_rule = next((r for r in cfg.get("ai_email_sources", [])
                        if re.search(r["from"], sender)), None)
        if ai_rule:
            if not re.search(ai_rule.get("skip_subject", "$^"), subject, re.I) and html_body:
                from pages import link_cards
                ai_emails.append((gid, ai_rule["name"],
                                  link_cards(html_body, ai_rule.get("base_url", "https://example.com/"))))
            elif not args.file:
                state["processed"][gid] = time.time()
            continue
        if sender.endswith("facebookmail.com"):
            from posts import facebook_post_from_email
            post = facebook_post_from_email(gid, sender, subject, html_body, text_body)
            if post and post["id"] not in state.get("posts_seen", {}):
                # Left unprocessed until the AI has read it, so a burst bigger
                # than one run's AI allowance carries over instead of being lost.
                fb_posts.append(post)
            elif not args.file:
                state["processed"][gid] = time.time()
            continue
        cards = parse_email(cfg, sender, subject, html_body, text_body)
        if cards is None:
            if args.verbose:
                log(f"skip {sender} · {subject[:60]!r} (not a saved-search alert)")
            if not args.file:
                state["processed"][gid] = time.time()
            continue
        log(f"{sender} · {subject[:70]!r}: {len(cards)} listings")
        for c in cards:
            consider_card(c)
        if not args.file:
            state["processed"][gid] = time.time()

    # Structured feeds (e.g. Nooklyn's JSON): same rules, no AI needed.
    if not args.file and not args.no_posts:
        from feeds import check_feeds
        for c in check_feeds(cfg, state, log):
            consider_card(c)

    if not args.no_posts:
        hits, broken = collect_posts(cfg, state, fb_posts, args.verbose, ai_emails, args.file)
        for hit in hits:
            add(hit)
        if broken:
            log(f"AI reader broken: {broken}")
            alert_problem(state, topic, "anthropic", "⚠️ Apartment watcher: AI post reader stopped",
                          f"Anthropic API {broken}.\nReddit / Listings Project / Facebook posts "
                          "aren't being read until this is fixed.", args.dry_run)

    send_reminders(cfg, state, topic, args.dry_run or args.file)

    sent = 0
    for key, hit in batch.items():
        prev = state["notified"].get(key)
        # Re-alert only if the price dropped since we last told you.
        if prev and (hit["price"] is None or prev["price"] is None or hit["price"] >= prev["price"]):
            continue
        if not args.seed:
            drafted = False
            if hit.get("takeover") and cfg.get("takeover", {}).get("outreach"):
                if args.dry_run or args.file:
                    subj, _ = outreach_text(cfg, hit)
                    log(f"  (would draft: {subj!r})")
                else:
                    drafted = create_draft(cfg, hit)
            notify_hit(topic, hit, args.dry_run, drafted=drafted)
            sent += 1
        state["notified"][key] = {"price": hit["price"], "ts": time.time()}

    log(f"{len(batch)} listings passed filters, {sent} notified"
        + (" (seed: none sent)" if args.seed else ""))
    if not args.dry_run and not args.file:
        save_state(state_path, state)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.inbox.json")
    ap.add_argument("--state", default="inbox_state.json")
    ap.add_argument("--days", type=int, default=2, help="how far back to read email")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--no-posts", action="store_true", help="skip Reddit/Listings Project/AI")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--dump")
    ap.add_argument("--file", nargs="+")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()

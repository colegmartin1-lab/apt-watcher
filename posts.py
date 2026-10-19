"""
Free-text sources: posts written by people (Reddit, Listings Project,
Facebook group notification emails). Keyword rules either miss these or let
junk through, so each new post is read by Claude Haiku, which answers: is this
a whole apartment being OFFERED (not "looking for", not a question), and what
are the price, size, neighborhood and lease type?

The answer is then judged by the same rules as the alert-email listings.
"""

import html
import re
import time
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import List, Literal, Optional

BROWSER_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate",
}
MAX_POST_CHARS = 3000


def _text(fragment):
    soup = BeautifulSoup(fragment, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


# --------------------------------------------------------------------------
# fetchers → list of {"id", "url", "source", "title", "text"}
# --------------------------------------------------------------------------

def fetch_reddit(cfg):
    """One request for all subreddits (a 'multireddit'): Reddit answers the
    first request from GitHub's servers but 429s the second, so never make
    two. Uses title AND body — the old watcher only read titles."""
    subs = "+".join(cfg["subreddits"])
    r = requests.get(f"https://www.reddit.com/r/{subs}/new/.rss?limit=50",
                     headers={"User-Agent": "apt-watcher/2.0 (personal apartment alerts)"},
                     timeout=20)
    r.raise_for_status()
    posts = []
    for e in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        link = re.search(r'<link[^>]*href="([^"]+)"', e)
        title = re.search(r"<title>(.*?)</title>", e, re.S)
        body = re.search(r"<content[^>]*>(.*?)</content>", e, re.S)
        sub = re.search(r'<category term="([^"]+)"', e)
        if not link or not title:
            continue
        url = link.group(1).split("?")[0]
        posts.append({
            "id": "reddit:" + url,
            "url": url,
            "source": f"r/{sub.group(1)}" if sub else "Reddit",
            "title": html.unescape(title.group(1)).strip(),
            "text": _text(html.unescape(body.group(1)))[:MAX_POST_CHARS] if body else "",
        })
    return posts


def fetch_listingsproject(cfg, seen_ids, max_pages=12, max_details=40):
    """NYC rentals category, page by page. The weekly batch drops on Wednesday
    morning (~80 NYC rentals over ~7 pages), so keep paging until a page has
    nothing new; on other days that's a single request. Detail pages are only
    fetched for listings not read yet (the slug alone hides 'Seeking' vs
    'Offering' and the rent)."""
    base = "https://www.listingsproject.com"
    urls = []
    for page in range(1, max_pages + 1):
        r = requests.get(cfg["listingsproject_url"], params={"page": page} if page > 1 else None,
                         headers=BROWSER_UA, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        found = list(dict.fromkeys(
            (a["href"] if a["href"].startswith("http") else base + a["href"]).split("?")[0]
            for a in soup.find_all("a", href=True) if "/listings/" in a["href"]))
        fresh = [u for u in found if "lp:" + u not in seen_ids and u not in urls]
        urls += fresh
        if not found or not fresh:
            break
        time.sleep(0.5)
    posts = []
    for url in urls[:max_details]:  # the rest wait for the next run
        try:
            d = requests.get(url, headers=BROWSER_UA, timeout=20)
            d.raise_for_status()
        except requests.RequestException:
            continue
        text = _text(d.text)
        title = text.split(" | Listings Project")[0][:140]
        posts.append({"id": "lp:" + url, "url": url, "source": "Listings Project",
                      "title": title, "text": text[:MAX_POST_CHARS],
                      "sublet_context": cfg["listingsproject_url"].endswith("/sublets")})
        time.sleep(0.5)
    return posts


CL_PRICE = re.compile(r"\$\s*([\d,]+)")


def fetch_craigslist(clcfg, seen_ids, place_check, min_price, max_price, log, max_details=40,
                     category="sub", label="Craigslist sublets", sublet_context=True):
    """Craigslist "sublets & temporary" search pages (no-JS fallback markup).

    Cheap filters run before any AI: price in budget, monthly (not daily/
    weekly) rent, and the post's own map pin inside an allowed neighborhood
    (place_check(lat, lon) → (geo, reason)). Posts failing them are returned
    in `skipped` so they're marked seen and never fetched again."""
    posts, skipped, details = [], [], 0
    for area in clcfg["areas"]:
        url = f"https://newyork.craigslist.org/search/{area}/{category}"
        r = requests.get(url, params={"min_price": int(min_price * 0.8), "max_price": max_price,
                                      "sort": "date"}, headers=BROWSER_UA, timeout=20)
        r.raise_for_status()
        for li in BeautifulSoup(r.text, "html.parser").select("li.cl-static-search-result"):
            a = li.find("a", href=True)
            if not a:
                continue
            link = a["href"].split("#")[0]
            pid = "cl:" + link.rstrip("/").split("/")[-1].split(".")[0]
            if pid in seen_ids or any(p["id"] == pid for p in posts) or pid in skipped:
                continue
            price_el = li.select_one(".price")
            price = CL_PRICE.search(price_el.get_text()) if price_el else None
            if price and not (min_price * 0.8 <= int(price.group(1).replace(",", "")) <= max_price):
                skipped.append(pid)  # e.g. a $130/night stay
                continue
            if details >= max_details:
                continue  # the rest wait for the next run
            details += 1
            try:
                d = requests.get(link, headers=BROWSER_UA, timeout=20)
                d.raise_for_status()
            except requests.RequestException:
                continue
            ds = BeautifulSoup(d.text, "html.parser")
            attrs = " · ".join(x.get_text(" ", strip=True) for x in ds.select(".attrgroup span"))
            if re.search(r"rent period:\s*(daily|weekly)", attrs, re.I):
                skipped.append(pid)
                continue
            m = ds.select_one("#map")
            geo = None
            if m and m.get("data-latitude") and m.get("data-longitude"):
                geo, why = place_check(float(m["data-latitude"]), float(m["data-longitude"]))
                if why:
                    skipped.append(pid)
                    continue
            body = ds.select_one("#postingbody")
            title = ds.select_one("#titletextonly")
            text = re.sub(r"\s+", " ", body.get_text(" ")).replace("QR Code Link to This Post", "") if body else ""
            listed = f"Listed price: {price_el.get_text(strip=True)}\n" if price_el else ""
            where_posted = "sublets & temporary" if category == "sub" else \
                "apartments by owner" if category == "abo" else "apartments / housing for rent"
            posts.append({"id": pid, "url": link, "source": label,
                          "title": (title.get_text(strip=True) if title else a.get_text(strip=True))[:140],
                          "text": f"Posted in Craigslist '{where_posted}'.\n{listed}{attrs}\n{text}"[:MAX_POST_CHARS],
                          "geo": geo, "sublet_context": sublet_context})
            time.sleep(0.5)
    log(f"{label}: {len(posts)} to read, {len(skipped)} filtered out cheaply")
    return posts, skipped


FB_LINK = re.compile(r"https?://(?:www\.|m\.|web\.)?facebook\.com/[^\s\"'<>]+")
FB_SKIP_LINK = re.compile(r"settings|unsubscribe|/notifications|/help|privacy|login|recover|/policies|"
                          r"/legal|email_notif|/mobile|/download|/messages|/friends", re.I)
FB_POST_LINK = re.compile(r"/posts/|permalink|story_fbid|story\.php|/groups/|/photo|/share/|pfbid", re.I)
# Only ever read post notifications; never security/login/account mail.
FB_SENDERS = re.compile(r"^(notification|groupupdates|update|updates|notify)\b", re.I)
FB_SENSITIVE = re.compile(r"\bcode\b|log ?in|password|security|confirm|verif|account|recover|"
                          r"two-factor|2fa|authenticat|payment|receipt|birthday|friend request|"
                          r"memories|messenger|tagged you|reacted", re.I)
FB_GROUP_NAME = re.compile(r"(?:posted|shared [^.]*?|new posts?) in (.+?)\s*$", re.I)
FB_PAGE_NAME = re.compile(r"^(.+?) (?:posted|shared|added|updated)\b", re.I)


def facebook_post_from_email(gid, sender, subject, html_body, text_body):
    """A Facebook notification email about a new post in a group, or from a
    page/profile you follow → one post for the AI. None for anything else,
    and never for security/login/account emails (those must not leave Gmail)."""
    if not FB_SENDERS.search(sender.split("@")[0]) or FB_SENSITIVE.search(subject):
        return None
    raw = html.unescape(html_body or text_body)
    links = []
    for l in FB_LINK.findall(raw):
        # Facebook wraps links as facebook.com/n/?<path>&aref=...; unwrap them.
        m = re.match(r"(https?://[^/]+)/n/\?(.+)", l)
        if m:
            l = f"{m.group(1)}/{unquote(m.group(2)).split('&')[0]}"
        if not FB_SKIP_LINK.search(l):
            links.append(l)
    post_links = [l for l in links if FB_POST_LINK.search(l)]
    if not post_links:
        return None
    url = post_links[0].split("&aref")[0].split("&__")[0]
    place = FB_GROUP_NAME.search(subject) or FB_PAGE_NAME.search(subject)
    where = place.group(1) if place else None
    text = _text(html_body) if html_body else re.sub(r"\s+", " ", text_body)
    return {"id": "fb:" + gid, "url": url,
            "source": f"FB · {where.strip()[:40]}" if where else "Facebook",
            "title": subject[:140], "text": text[:MAX_POST_CHARS]}


# --------------------------------------------------------------------------
# AI reader
# --------------------------------------------------------------------------

class TakeoverSignals(BaseModel):
    """Read on every post: used to drop scams everywhere, and to decide
    whether a sublet is worth asking about a lease takeover."""
    lister_type: Literal["tenant", "landlord", "broker", "host_or_operator", "unknown"] = Field(
        description="Who is posting: the current tenant; the landlord/management; a broker/agent; "
                    "or a furnished-rental host/operator (Airbnb-style, corporate housing, co-living)")
    tenant_returning: Optional[bool] = Field(
        description="True if the tenant says they are coming back (e.g. 'while I'm away until March'); "
                    "False if they are leaving for good (relocating, moving out, breaking lease); null if unclear")
    scam_risk: Literal["low", "medium", "high"] = Field(
        description="Likelihood this is a rental scam. Red flags: rent far below the area for that size, "
                    "lister abroad / 'keys by mail', deposit or payment before viewing, Zelle/wire/gift "
                    "cards/crypto, contact only via WhatsApp/text/off-platform email, no real address or "
                    "viewing, pressure/urgency, generic copy-paste text, too-good-to-be-true luxury")
    scam_flags: List[str] = Field(description="Up to 3 short red flags actually present (empty if none)")
    takeover_odds: Literal["low", "medium", "high"] = Field(
        description="Chance the place could become a lease takeover/assignment for a new long-term "
                    "tenant. High: tenant leaving for good, mentions breaking/transferring the lease, "
                    "landlord open to a new tenant, many months left. Low: tenant returning, short "
                    "fixed dates, furnished with tenant's belongings staying, host/operator listing, room.")
    takeover_reasons: List[str] = Field(description="Up to 2 short reasons for takeover_odds")
    contact_email: Optional[str] = Field(description="An email address written in the post itself, else null")


class PostReading(TakeoverSignals):
    is_offer: bool = Field(description="True only if someone is OFFERING a place to rent. "
                           "False for 'looking for'/ISO posts, questions, rants, news, ads for services.")
    kind: Literal["whole_apartment", "room_in_shared", "other"]
    price_monthly: Optional[int] = Field(description="Monthly rent in USD, or null if not stated")
    bedrooms: Optional[int] = Field(description="0 for studio/alcove studio, else bedroom count; null if unknown")
    neighborhood: Optional[str] = Field(description="NYC neighborhood as stated or clearly implied, e.g. 'Park Slope'")
    address: Optional[str] = Field(description="Street address or cross streets if given, e.g. '448 Bergen St' or 'Bedford Ave & N 7th St'")
    lease: Literal["lease", "lease_takeover", "long_sublet", "short_term", "unknown"] = Field(
        description="lease = new lease with the landlord; lease_takeover = taking over the "
                    "remaining lease in your own name; long_sublet = subletting from the tenant for "
                    "3+ months; short_term = under ~3 months, nightly/weekly rates, or furnished stays")
    available: Optional[str] = Field(description="Move-in date as written, if stated")
    summary: str = Field(description="One line, max 80 chars, the key facts a renter would want")


READER_PROMPT = """You read NYC housing posts for one renter and extract facts. \
Be literal: only report what the post says or clearly implies. A post titled \
"Looking for…", "ISO", "Seeking…" is not an offer even if it names a price. \
A "room" in a shared apartment is kind=room_in_shared even if the room is large. \
Judge scam risk and lease-takeover odds from the post's own words; don't invent \
red flags or reasons that aren't there."""


class Reader:
    def __init__(self, cfg, log):
        import anthropic  # only needed when free-text sources are on
        self.anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=3)
        self.model = cfg.get("ai_model", "claude-haiku-5-5")
        self.budget = cfg.get("ai_max_posts_per_run", 60)
        self.log = log
        self.broken = None  # set when the key itself is bad: stop and tell the phone

    def read(self, post):
        if self.budget <= 0:
            return None
        self.budget -= 1
        content = f"Source: {post['source']}\nTitle: {post['title']}\n\n{post['text']}"
        return self.call(READER_PROMPT, content, PostReading, label=post["url"])

    def call(self, system, content, schema, max_tokens=2048, label=""):
        """One structured-output request; None on any failure. A bad key or an
        empty balance also sets self.broken so the run can warn the phone."""
        try:
            resp = self.client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                output_config={"effort": "low"},
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=schema,
            )
        except (self.anthropic.AuthenticationError, self.anthropic.PermissionDeniedError) as e:
            self.broken, self.budget = f"key rejected ({e.status_code}): expired, revoked or wrong", 0
            return None
        except self.anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                self.broken, self.budget = "out of credit: top up at console.anthropic.com", 0
            else:
                self.log(f"  AI read failed (400) for {label}: {e.message[:120]}")
            return None
        except self.anthropic.APIStatusError as e:
            self.log(f"  AI read failed ({e.status_code}) for {label}")
            return None
        except self.anthropic.APIConnectionError:
            self.log(f"  AI read failed (network) for {label}")
            return None
        if resp.stop_reason in ("refusal", "max_tokens") or resp.parsed_output is None:
            return None
        return resp.parsed_output

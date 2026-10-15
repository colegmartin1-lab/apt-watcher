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

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
from typing import Literal, Optional

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


def fetch_listingsproject(cfg, seen_ids):
    """Index page lists the newest listings; detail text is only fetched for
    ones we haven't read yet (the slug alone hides 'Seeking' vs 'Offering')."""
    base = "https://www.listingsproject.com"
    r = requests.get(cfg["listingsproject_url"], headers=BROWSER_UA, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    urls = list(dict.fromkeys(
        (a["href"] if a["href"].startswith("http") else base + a["href"]).split("?")[0]
        for a in soup.find_all("a", href=True) if "/listings/" in a["href"]))
    posts = []
    for url in urls:
        pid = "lp:" + url
        if pid in seen_ids:
            continue
        try:
            d = requests.get(url, headers=BROWSER_UA, timeout=20)
            d.raise_for_status()
        except requests.RequestException:
            continue
        text = _text(d.text)
        title = text.split(" | Listings Project")[0][:140]
        posts.append({"id": pid, "url": url, "source": "Listings Project",
                      "title": title, "text": text[:MAX_POST_CHARS]})
        time.sleep(0.5)
    return posts


FB_GROUP_LINK = re.compile(r"https://(?:www\.|m\.)?facebook\.com/groups/[^\s\"'<>]+")


def facebook_post_from_email(gid, subject, html_body, text_body):
    """A Facebook group-notification email → one post, or None if the email
    isn't about a group post (friend activity, login alerts, ...)."""
    raw = html_body or text_body
    links = FB_GROUP_LINK.findall(raw)
    if not links:
        return None
    # Prefer the link to the specific post over the group's front page.
    post_links = [l for l in links if re.search(r"/(posts|permalink)/", l)]
    url = html.unescape((post_links or links)[0]).split("&")[0]
    text = _text(html_body) if html_body else re.sub(r"\s+", " ", text_body)
    return {"id": "fb:" + gid, "url": url, "source": "Facebook group",
            "title": subject[:140], "text": text[:MAX_POST_CHARS]}


# --------------------------------------------------------------------------
# AI reader
# --------------------------------------------------------------------------

class PostReading(BaseModel):
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
A "room" in a shared apartment is kind=room_in_shared even if the room is large."""


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

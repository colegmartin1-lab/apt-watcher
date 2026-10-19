# apt-watcher

Long-term rental tracker (`inbox_watcher.py`, rebuilt Oct 2026). Runs on GitHub
Actions and pushes to ntfy. The main search is leases and lease takeovers.

**Takeover leads (added 2026-10-19).** Sublet and temporary posts can also lead
to a lease takeover, when the tenant is leaving for good. Sources: Craigslist
"sublets & temporary" (Brooklyn and Manhattan), r/NYCsublets, Listings Project
sublets, Leasebreak emails and Facebook group emails. Every post gets a scam-risk
and takeover-odds score from Haiku, and high scam risk is dropped everywhere.
A sublet becomes a "🔁 Takeover lead" alert when it is a whole apartment in the
area and budget, the tenant isn't coming back, the odds are at least
`takeover.min_odds`, and the scam risk is at most `takeover.max_scam`. Each lead
also saves a lease-takeover message to Gmail **Drafts**, using the
`takeover.outreach` template. Nothing is ever sent automatically.

The search: solo studio/1BR, $1,000–3,000, in Greenpoint, Williamsburg (north of
Domino Park, west of the BQE), Fort Greene, Clinton Hill, Boerum Hill, Cobble
Hill, Brooklyn Heights, Prospect Heights, Park Slope (not South Slope), West
Village, or SoHo. All of it lives in `config.inbox.json`.

**Why it reads email instead of scraping.** StreetEasy, Zillow, Trulia and the
other big sites block bots, but they all email saved-search alerts. So every run:

1. Reads recent alert emails from Gmail over IMAP (app password, read-only).
   Only **saved-search** alerts are read. Recommendation emails like StreetEasy
   "Homes for You", Zillow `recommendations@`, Trulia "More homes you might
   love" and RentHop are skipped. Rules are in `email_sources` in the config.
2. Pulls out each listing: address, unit, price, beds and link.
3. Looks the address up in NYC's free GeoSearch to get the real neighborhood
   and coordinates, instead of trusting the email's label.
4. Drops rooms, for-sale homes, 2BR+, listings outside the budget, rented
   listings and listings in the wrong neighborhood.
5. Merges duplicates, so one apartment listed on Trulia, Zillow and
   StreetEasy becomes one alert.
6. **Free-text posts:** reads Reddit (r/NYCapartments and r/BrooklynApartments,
   title *and* body), Listings Project, and
   Facebook-group notification emails. Claude Haiku decides whether each post
   offers a whole apartment (not "looking for…"), and pulls out price, size,
   neighborhood and lease type. Costs about $1/month. This step is skipped if
   `ANTHROPIC_API_KEY` isn't set.
7. **Broker & landlord websites** (`pages` in the config, about 50 of them):
   boutique brownstone-Brooklyn brokers, landlords that rent directly, and
   AppFolio/Buildium/RentCafe pages of small managers with no website. Each
   page is checked every 15–30 minutes. In link mode, only links new since the
   last check go to Haiku. In text mode (single-building pages that list units
   as text), the priced text is watched and read again when it changes. A
   page's first check records what's there silently. `skip_cards_matching`
   ignores sidebars that reshuffle on every load.
8. **Structured feeds** (`feeds`): Nooklyn's JSON search API and Brooklyn
   Group's embedded page data. Their listings already come with price, beds
   and address, so they go through the same rules as the email alerts, with no
   AI needed.
9. Pushes each match to ntfy once. It pushes again only if the price drops.
   If Gmail stops working, your phone gets a ⚠️ alert (at most every 12h).

State (handled email IDs, sent listings, read posts) is in `inbox_state.json`.
It holds hashes and IDs only, and is committed back after each run.

**Secrets** (repo → Settings → Secrets → Actions): `GMAIL_APP_PASSWORD`,
`ANTHROPIC_API_KEY`, `NTFY_TOPIC`. To run locally, put `KEY=value` lines in
`~/.apt-watcher.env`.

**Commands**
```
python inbox_watcher.py --dry-run -v      # show every decision, send nothing
python inbox_watcher.py --seed            # mark everything current as seen, silently
python inbox_watcher.py --days 7 --dump DIR   # save raw alert emails (for adding a site)
python inbox_watcher.py --file DIR/*.eml --dry-run -v --no-posts   # test parsers on saved emails
```

**Adding a broker/landlord site:** add `{"name", "url", "every_minutes"}` to
`pages` (use `"mode": "text"` if units aren't links). The first run records it
silently.

**Adding a site's alerts:** dump a sample email. Then add an `email_sources`
rule with the sender, the line that starts each listing card, and the line
where the saved-search part ends.

**Schedule:** GitHub's own cron runs hours late, so cron-job.org calls the
workflow's `workflow_dispatch` every 5 minutes. The `*/15` cron is only a backup.

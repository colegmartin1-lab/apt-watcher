# apt-watcher

Long-term rental tracker (`inbox_watcher.py`, rebuilt Oct 2026). Runs on GitHub
Actions and pushes to ntfy. Leases and lease takeovers only: sublets and short
stays are always dropped. The old sublet tracker was retired on 2026-10-14.

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
7. Pushes each match to ntfy once. It pushes again only if the price drops.
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

**Adding a site's alerts:** dump a sample email. Then add an `email_sources`
rule with the sender, the line that starts each listing card, and the line
where the saved-search part ends.

**Schedule:** GitHub's own cron runs hours late, so cron-job.org calls the
workflow's `workflow_dispatch` every 5 minutes. The `*/15` cron is only a backup.

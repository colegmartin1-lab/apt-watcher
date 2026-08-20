# apt-watcher v3

Watches Craigslist across Williamsburg, Greenpoint, Fort Greene, Prospect
Heights, Brooklyn Heights, Cobble Hill, Boerum Hill, and Park Slope for
3BR+ under $7,000. New listings ping your phone AND your Mac.

Your ntfy topic is already configured: shmole-burkin-zest-apt-2027

**Status: RUNNING IN THE CLOUD (24/7).** As of 2026-07-18 the watcher runs
every ~5 minutes on GitHub Actions — your Mac can be asleep, closed, or off:
https://github.com/colegmartin1-lab/apt-watcher

The cloud repo is public; your ntfy topic is stored as a private repo secret
(NTFY_TOPIC), not in the code. The seen-listings DB lives in the repo and is
committed back after each run, so dedupe persists across runs.

The old local launchd job is installed but UNLOADED to avoid duplicate
notifications. To fall back to Mac-only polling:
  launchctl load ~/Library/LaunchAgents/com.cole.aptwatcher.plist
If you change search criteria, edit config.json in the CLOUD repo (or edit
here and push) — this local copy no longer drives notifications.

## Reality check on coverage

**What the bot watches (5 Craigslist searches, all confirmed working):**
- Williamsburg/Greenpoint apartments, 3BR+
- Brownstone Brooklyn apartments (Ft Greene through Park Slope), 3BR+
- Sublets/lease-takeovers in BOTH areas (this is where lease breaks post)
- Brooklyn-wide 4BR+ (catches listings with sloppy neighborhood tags)

All searches use `sort=date` so brand-new posts are always on page 1.
(The old no-broker-fee search was removed: it was a strict subset of the
main Wburg/Greenpoint search, so dedupe meant it could never fire.)

Craigslist has NO native alerts, which is exactly why a bot here is worth
having — and it's where you found the Devoe listing at 4am.

**What blocks bots (403 Forbidden) — use native alerts instead:**
RentHop, Leasebreak, StreetEasy, Zillow, Trulia, HotPads, Apartments.com,
Redfin, Zumper. These are left in config.json with "enabled": false so
you can see what was tried. Don't waste time fighting them; their own
instant alerts are fast and free:

- **StreetEasy** — most important one in NYC. Saved search, alerts ON,
  frequency INSTANT. Most listings hit SE before anywhere else.
- **Zillow** — one saved search also covers Trulia and HotPads (same backend).
- **RentHop, Zumper, Apartments.com** — saved-search alerts in-app.
- **Facebook groups** (Gypsy Housing, BK Housing) — manual, no automation possible.

## Getting StreetEasy/Zillow into the SAME phone feed (no scraping)

ntfy accepts messages by email: anything sent to
`ntfy-shmole-burkin-zest-apt-2027@ntfy.sh` is pushed to your topic.
So instead of scraping the blocked sites, route their alert emails there:

1. On StreetEasy/Zillow/etc., create the saved search with EMAIL alerts,
   frequency instant.
2. In Gmail: Settings → Forwarding → add forwarding address
   `ntfy-shmole-burkin-zest-apt-2027@ntfy.sh`. Gmail's confirmation code
   will arrive as an ntfy notification on your phone — enter it back in Gmail.
3. Create a Gmail filter (e.g. `from:(streeteasy.com OR zillow.com)`) →
   "Forward to" that address.

Result: every listing site lands in one ntfy feed. Bot owns Craigslist,
native alerts own everything else, all in one place.

## Daily use

Once scheduled (below), you do nothing. Notifications arrive on your phone
and as Mac banners. Useful commands:

```
python3 apt_watcher.py           # run one cycle now
python3 apt_watcher.py --test    # test notifications
python3 apt_watcher.py --list    # everything seen, newest first
```

## Scheduling (ALREADY DONE — runs every 5 min automatically)

The plist is installed at ~/Library/LaunchAgents/com.cole.aptwatcher.plist
and loaded. The copy in this folder matches the installed one. Note: the
installed plist hardcodes this folder's exact path ("apt-watcher 3", with
the space) — if you rename or move the folder, edit both copies and rerun
the install commands below.

- Reinstall after changes:
  ```
  cp com.cole.aptwatcher.plist ~/Library/LaunchAgents/
  launchctl unload ~/Library/LaunchAgents/com.cole.aptwatcher.plist 2>/dev/null
  launchctl load ~/Library/LaunchAgents/com.cole.aptwatcher.plist
  ```
- Verify it's running: `tail watcher.log` (new lines every ~5 min).
- To stop: `launchctl unload ~/Library/LaunchAgents/com.cole.aptwatcher.plist`

## The sublets tracker (second, independent tracker)

`apt_watcher.py` drives two trackers. They share code but nothing else — own
config, own DB, own ntfy topic — so their alerts never mix:

| | long-term | sublets |
|---|---|---|
| config | `config.json` | `config.sublets.json` |
| DB | `seen_listings.db` | `seen_sublets.db` |
| topic | `cole-solo-nyc-2026-q7x9m` | `cole-sublets-bk-2026-v4k2p` |
| push reads | `New: ...` | `Sublet: ...` |

Run it:
```
python3 apt_watcher.py --config config.sublets.json --db seen_sublets.db
```

Brief: whole apartment only (no roommates/room shares), month-to-month,
<=$2,500, in Greenpoint, Williamsburg (N of the bridge, W of the BQE), Park
Slope, Fort Greene, Prospect Heights, Cobble Hill, Brooklyn Heights, Boerum
Hill and Red Hook.

### Sources (probed 2026-08-20)

- **Craigslist `sub`** — the "sublets & temporary" category, not `apa`. Three
  geo circles: Greenpoint/N-W Williamsburg, the brownstone belt, and Red Hook
  (which sits ~1.6mi from the brownstone circle's center, outside its radius,
  so it needs its own).
- **Sublet.com** — cards state unit type and term outright ("Apartment ...
  Month to Month" vs "Room Rental ... Min 6 Months"). Borough-wide with no
  neighborhood on the card, so the adapter reads each detail page for it.
  The site is slow and intermittently 504s; a failure just skips the cycle.
- **Listings Project**, **Reddit** — as in the long-term tracker.
- **403 (use their own alerts):** Leasebreak, FurnishedFinder, Flip.
- Craigslist `?query=` searches return a JS shell with zero parseable cards.
  Only category browsing works — hence `sub` rather than `apa?query=sublet`.

### Why the filters look the way they do

Craigslist's sublet category is roughly **half room shares**, so "whole
apartment only" is enforced on the title, and that filtering is the fiddly
part of this tracker:

- **`exclude_regex` exists because substrings collide.** `"room for rent"` as
  a plain substring also matches "1 bed**room for rent**" — a whole apartment.
  Every room-share phrase containing "room" is therefore a `\broom ...` regex,
  which can't match inside "bedroom". Plain substrings are only used for
  phrases that can't collide ("roommate", "loft share").
- **Sub-month stays are excluded** — "9 Days:", "two weeks", "Oct 1 - 15",
  weekly rates. The day pattern stops at 27 so "30 day notice" survives.
- **`prefer_keywords` is a highlight, not a filter.** Requiring "month to
  month" in the title would drop most real listings, since plenty of
  open-ended sublets never say it. Matches get a ⭐ and a star tag instead.
- **Bedroom count is deliberately unconstrained.** A whole 2BR at <=$2,500 is
  a win, and room-shares are caught by keyword, not bed count.
- **Carroll Gardens is deliberately not excluded** — it sits between Cobble
  Hill and Red Hook, so a listing tagged there is almost always on a wanted
  border. Add it to `exclude_locations` to change that.

After editing filters, re-seed so pre-existing listings don't all fire at once:
```
python3 apt_watcher.py --config config.sublets.json --db seen_sublets.db --seed
```


## Troubleshooting

- **"0 found" on a Craigslist source every cycle** — CL changed markup, or
  that search URL returns nothing. Paste the URL in a browser to check.
- **[BLOCKED] messages** — that site refuses bots. Set "enabled": false for
  it in config.json and set up its native alert instead.
- **No phone notification** — confirm the topic in config.json exactly
  matches the topic you subscribed to in the ntfy app.
- **Mac asleep = no polling.** For true 24/7 (including 4am), run the same
  folder on a cheap VPS with cron:
  `*/5 * * * * cd ~/apt-watcher && python3 apt_watcher.py >> watcher.log 2>&1`

## Tuning

Edit config.json:
- `max_price` / `min_price` — currently $2,500–$7,000
- `exclude_keywords` — drops studios, 1BR, 2BR, roommate posts
- Add sources: build any Craigslist search in your browser, copy the URL,
  add a new block with "type": "craigslist"

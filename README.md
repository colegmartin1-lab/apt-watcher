# apt-watcher (cloud)

Cloud copy of a personal Craigslist apartment watcher. Runs every ~5 minutes
via GitHub Actions, dedupes against `seen_listings.db` (committed back after
each run), and pushes new listings to an ntfy topic held in the `NTFY_TOPIC`
repository secret.

Not affiliated with Craigslist. Personal, low-volume use.

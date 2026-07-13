# Hyderabad Buy/Sell Tracker

Polls r/HyderabadBuySell and r/HyderabadUsedItems every 30 minutes via
GitHub Actions and reports new mobile/laptop/tablet/console listings
**from today only**.

## Files

- `tracker.py` — the script (stdlib only, no dependencies to install)
- `.github/workflows/tracker.yml` — runs it every 30 min
- `seen_ids.json` — dedup state, committed back by the workflow after each run
- `report.md` — latest run's report, overwritten each run and committed

## Setup

1. Create a new GitHub repo and push these files (keep the folder structure,
   especially `.github/workflows/tracker.yml`).
2. (Optional, for push notifications) Add a repo secret:
   - Settings → Secrets and variables → Actions → New repository secret
   - Name: `NTFY_TOPIC`
   - Value: any topic string you'll subscribe to in the ntfy app, e.g. `hyd-buysell-abc123xyz`
   - If you skip this, the workflow still runs fine — it just won't push
     notifications. The report is still written to `report.md` and printed
     in the Actions log each run.
3. Actions run automatically every 30 minutes once the workflow file is on
   the default branch. You can also trigger a run manually from the
   Actions tab → "Hyderabad Buy/Sell Tracker" → "Run workflow".

## "Only today" behavior

`tracker.py` has:

```python
ONLY_TODAY = True
MAX_AGE_HOURS = 24
```

Any post created before the current UTC calendar date is skipped — even if
it's technically "new" to `seen_ids.json` (e.g. the very first run, or after
a gap in scheduling). It's still added to `seen_ids.json` so it won't be
reconsidered later, it's just never reported. Set `ONLY_TODAY = False` if
you ever want to fall back to reporting everything unseen regardless of age.

Note: GitHub Actions cron is UTC-based, and the "today" check also uses UTC,
so the cutover to a new "day" happens at 00:00 UTC (5:30 AM IST), not at
midnight IST. Let me know if you'd rather have the day boundary align to IST
instead — that's a one-line change (offset the `datetime.now()` calls by
+5:30) if you want it.

## Notes

- Uses unauthenticated `reddit.com/.json` endpoints — no API key needed, but
  Reddit may rate-limit if requests look automated. 30-min interval with a
  descriptive User-Agent (already set) should be safe.
- `git push` at the end of the workflow requires `permissions: contents: write`,
  which is already set in the workflow file.

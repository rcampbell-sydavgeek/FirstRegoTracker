# Sydney New-Rego Watcher — Setup Guide

Flags brand-new Australian registrations (VH- marks never seen in CASA's
register before) and alerts you when one is flying or parked within 60 km
of Sydney Airport — for JetPhotos "first rego" hunting.

## How it works

1. About once a day it downloads CASA's public register (`acrftreg.csv`)
   and compares it with every mark it has ever seen. A mark it has never
   seen is a **new-rego candidate**. The very first run only records a
   baseline (you'll get one "watcher is live" notification) — new marks are
   flagged from the next daily refresh onwards.
2. It tries to find each candidate's ICAO24 hex (hexdb.io / adsbdb.com).
   New marks often aren't in those databases for days or weeks.
3. Every 5 minutes it checks OpenSky for aircraft within 60 km of YSSY:
   - **Candidate in range** → high-priority "NEW REGO near Sydney" alert.
   - **Australian-range hex that no database can name** → low-priority
     "Unidentified VH aircraft" alert. This catches a brand-new aircraft
     before the databases know it. If it's too noisy, add
     `ALERT_UNIDENTIFIED: "false"` to the workflow's `env:` block (or just mute the low-priority alerts in the ntfy app).
   - RAAF military hex block is ignored.

## Setup

1. New **private** GitHub repo, e.g. `syd-new-rego-watcher`.
2. Upload everything in this folder, including the `.github` folder
   (`watch.yml` must end up at `.github/workflows/watch.yml`).
3. **Settings → Secrets and variables → Actions** (not "Agents"):
   - `NTFY_TOPIC` — your ntfy topic (same as your others or a new one)
   - `CASA_CSV_URL` — *optional*. Leave it out and the script finds the
     download link on CASA's data-files page itself. Only add it (direct
     link to `acrftreg.csv` or its `.zip`) if the first run's log says it
     couldn't find the link.
4. **Actions** tab → run the workflow once manually. Check the log shows
   `Register: NNNNN marks. Baseline run: True` and you get the "live" push.
5. Add the repo to your GitHub token's repository list, then create a
   cron-job.org job exactly like your other watchers (POST to
   `https://api.github.com/repos/YOUR_USERNAME/syd-new-rego-watcher/actions/workflows/watch.yml/dispatches`,
   every 5 minutes, body `{"ref":"main"}`, expect `204`).

## Testing end to end

In `watch.yml`, add the line `TEST_REGISTRATION: VH-QPM` (any aircraft you
know flies near Sydney) to the "Run watcher" `env:` block. That
aircraft is then treated as a new rego, so you'll get a real alert the next
time it's near Sydney. Remove it afterwards.

## Limits worth knowing

- CASA's file only lists marks **currently** registered. A mark that was
  registered and cancelled before this watcher started could look "new".
  Alerts say "possible first rego" — confirm on JetPhotos before shooting.
  Marks that disappear and later return are logged as re-issued and not
  alerted.
- Many new VH- marks are light aircraft or helicopters that rarely come near
  Sydney; expect few alerts.
- I couldn't test against CASA's live file from my side, so the first real
  run is the real test. If the log shows a column or download error, send me
  the log lines (or a few rows of `acrftreg.csv`) and I'll adjust.

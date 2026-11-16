#!/usr/bin/env python3
"""
Sydney New-Registration ("First Rego") Watcher
-----------------------------------------------
Spots brand-new Australian registrations (VH-xxx marks that have never
appeared in the CASA register before) and sends a push notification via
ntfy.sh when one is seen flying or parked within RADIUS_KM of Sydney
Airport - for JetPhotos "first rego" hunting.

How it works
  1. About once a day, downloads CASA's public register file (acrftreg.csv)
     and compares it with every mark seen on previous days. A mark that has
     NEVER been seen before is a "new rego" candidate. (The very first run
     only records a baseline and alerts on nothing.)
  2. Candidates are resolved to an ICAO24 hex via hexdb.io / adsbdb.com.
     New marks often aren't in those databases yet, so:
  3. Every run, aircraft within RADIUS_KM of Sydney are pulled from OpenSky.
     - A candidate's hex in range -> alert.
     - An Australian-range hex that NO database can name -> low-priority
       "unidentified" alert (could be a brand-new aircraft not yet
       catalogued). OFF by default - turn on with ALERT_UNIDENTIFIED=true.

State lives in small JSON files committed back to the repo by the workflow.
"""

import csv
import io
import json
import math
import os
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Optional override. If blank, the script finds the acrftreg download link on
# CASA's data-files page itself.
CASA_CSV_URL = os.environ.get("CASA_CSV_URL", "").strip()
CASA_PAGE_URL = "https://www.casa.gov.au/aircraft/aircraft-registration/data-files-registered-aircraft"

# Optional: a registration to treat as a "new rego" for an end-to-end test,
# e.g. TEST_REGISTRATION=VH-QPM (any aircraft you know is near Sydney).
TEST_REGISTRATION = os.environ.get("TEST_REGISTRATION", "").strip().upper()

# Set FORCE_REGISTER_REFRESH=true (in watch.yml's env block) to ignore the
# once-a-day / once-an-hour timers and try the CASA download on every run.
# Remove it again once the baseline is recorded.
FORCE_REGISTER_REFRESH = os.environ.get("FORCE_REGISTER_REFRESH", "").strip().lower() == "true"

ALERT_UNIDENTIFIED = os.environ.get("ALERT_UNIDENTIFIED", "false").strip().lower() == "true"

CENTRE_NAME = "Sydney"
CENTRE_LAT = -33.9461   # YSSY
CENTRE_LON = 151.1772
RADIUS_KM = 60

REGISTER_REFRESH_HOURS = 20     # how often to re-download the CASA file
REGISTER_RETRY_HOURS = 1        # wait before retrying a failed download
RENOTIFY_AFTER_HOURS = 24       # same candidate re-alert gap
UNIDENTIFIED_RENOTIFY_DAYS = 7
HEX_RETRY_HOURS = 6             # re-try resolving a candidate's hex this often
FOUND_HEX_TTL_DAYS = 30
NOT_FOUND_HEX_TTL_HOURS = 24
MAX_HEX_LOOKUPS_PER_RUN = 15
CANDIDATE_KEEP_DAYS = 400

# ICAO24 block allocated to Australia (civil + military), and the RAAF
# sub-block which we never want to flag.
AU_HEX_RANGE = (0x7C0000, 0x7FFFFF)
AU_MIL_HEX_RANGE = (0x7CF800, 0x7CFAFF)

REGISTER_STATE_FILE = Path("register_state.json")   # present / ever_seen / timestamps
CANDIDATES_FILE = Path("candidates.json")           # mark -> info
NOTIFIED_FILE = Path("notified.json")               # key -> timestamp
HEX_CHECKED_FILE = Path("hex_checked.json")         # icao24 -> {reg, ts, alerted}

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
HEXDB_REG_TO_HEX = "https://hexdb.io/reg-hex?reg={reg}"
HEXDB_AIRCRAFT = "https://hexdb.io/api/v1/aircraft/{hex}"
ADSBDB_AIRCRAFT = "https://api.adsbdb.com/v0/aircraft/{key}"

HEADERS = {"User-Agent": "syd-new-rego-watcher/1.0"}

# casa.gov.au tends to stall requests that don't look like a browser.
CASA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,text/csv,application/zip,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}
CASA_TIMEOUT = (15, 90)   # (connect, read) seconds

# Manual fallback: if the download fails, a copy of the register committed to
# the repo root under one of these names is used instead.
LOCAL_REGISTER_FILES = ["acrftreg.csv", "acrftreg.zip"]

DAY = 86400
HOUR = 3600


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def normalize_mark(raw):
    """'abc' / 'VHABC' / 'VH-ABC' -> 'VH-ABC'. Returns '' if unusable."""
    m = (raw or "").strip().upper().replace(" ", "")
    if not m:
        return ""
    if m.startswith("VH") and "-" not in m and len(m) > 2:
        m = "VH-" + m[2:]
    elif "-" not in m:
        m = "VH-" + m
    return m


def is_au_civil_hex(icao24):
    try:
        v = int(icao24, 16)
    except (TypeError, ValueError):
        return False
    return AU_HEX_RANGE[0] <= v <= AU_HEX_RANGE[1] and not (
        AU_MIL_HEX_RANGE[0] <= v <= AU_MIL_HEX_RANGE[1]
    )


def send_ntfy(title, message, priority="high", tags="airplane"):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set - skipping push:", title, "|", message)
        return
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"ntfy sent OK: {title}")
        else:
            print(f"ntfy FAILED (status {resp.status_code}): {resp.text}")
    except requests.RequestException as e:
        print(f"ntfy FAILED (exception): {e}")


# ---------------------------------------------------------------------------
# CASA register
# ---------------------------------------------------------------------------

def discover_register_url():
    if CASA_CSV_URL:
        return CASA_CSV_URL
    resp = requests.get(CASA_PAGE_URL, headers=CASA_HEADERS, timeout=CASA_TIMEOUT)
    resp.raise_for_status()
    links = re.findall(r'href="([^"]*acrftreg[^"]*)"', resp.text, flags=re.I)
    if not links:
        raise RuntimeError(
            "Could not find an acrftreg download link on CASA's page. "
            "Set the CASA_CSV_URL secret to the direct link."
        )
    links.sort(key=lambda u: (u.lower().endswith(".zip"), len(u)))  # prefer plain .csv
    return urljoin(CASA_PAGE_URL, links[0].replace("&amp;", "&"))


def _decode_register_bytes(data):
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                raise RuntimeError("Zip contained no .csv file")
            data = zf.read(names[0])
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def download_register_text():
    try:
        url = discover_register_url()
        print(f"Downloading CASA register from {url}")
        resp = requests.get(url, headers=CASA_HEADERS, timeout=CASA_TIMEOUT)
        resp.raise_for_status()
        return _decode_register_bytes(resp.content)
    except Exception as e:
        for name in LOCAL_REGISTER_FILES:
            f = Path(name)
            if f.exists():
                print(f"CASA download failed ({e}); using local file {name} instead.")
                return _decode_register_bytes(f.read_bytes())
        raise


def _norm_header(h):
    h = re.sub(r"\s*\([A-Za-z]{1,3}\)\s*$", "", h or "")
    return h.strip().lower()


def _find_col(headers, *prefixes):
    normed = [_norm_header(h) for h in headers]
    for p in prefixes:
        for i, n in enumerate(normed):
            if n == p:
                return headers[i]
    for p in prefixes:
        for i, n in enumerate(normed):
            if n.startswith(p):
                return headers[i]
    return None


def parse_register(text):
    """Returns {mark: info dict}. Raises if no Mark column can be found."""
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    mark_col = _find_col(headers, "mark")
    if not mark_col:
        raise RuntimeError(f"No 'Mark' column in CASA file. Headers seen: {headers[:8]}")
    mfr_col = _find_col(headers, "manufacturer")
    model_col = _find_col(headers, "model")
    type_col = _find_col(headers, "icao type designator")
    first_col = _find_col(headers, "date first registered")

    marks = {}
    for row in reader:
        mark = normalize_mark(row.get(mark_col))
        if not mark:
            continue
        desc = " ".join(
            x for x in [(row.get(mfr_col) or "").strip(), (row.get(model_col) or "").strip()] if x
        )
        marks[mark] = {
            "desc": desc,
            "icao_type": (row.get(type_col) or "").strip() if type_col else "",
            "first_registered": (row.get(first_col) or "").strip() if first_col else "",
        }
    return marks


def diff_register(current, reg_state):
    """
    Returns (new_marks, returned_marks, updated_state, is_baseline).
    new_marks: never seen in any earlier snapshot (a true first use, as far
    as this watcher's history goes). returned_marks: previously seen, dropped
    off, and came back (a re-issued mark - NOT a JetPhotos first).
    """
    present = set(reg_state.get("present", []))
    ever = set(reg_state.get("ever_seen", []))
    cur = set(current)
    baseline = not ever

    if baseline:
        new_marks, returned = [], []
    else:
        new_marks = sorted(m for m in cur if m not in ever)
        returned = sorted(m for m in cur if m in ever and m not in present)

    reg_state["present"] = sorted(cur)
    reg_state["ever_seen"] = sorted(ever | cur)
    return new_marks, returned, reg_state, baseline


def maybe_refresh_register(reg_state, candidates, now):
    last_ok = reg_state.get("last_check", 0)
    last_try = reg_state.get("last_attempt", 0)
    if not FORCE_REGISTER_REFRESH:
        if now - last_ok < REGISTER_REFRESH_HOURS * HOUR:
            return
        if now - last_try < REGISTER_RETRY_HOURS * HOUR:
            return
    reg_state["last_attempt"] = now
    try:
        current = parse_register(download_register_text())
    except Exception as e:  # network, parsing, anything - never crash the run
        print(f"Register refresh FAILED (will retry): {e}")
        return
    if len(current) < 500:
        print(f"Register looks truncated ({len(current)} marks) - ignoring this download.")
        return

    new_marks, returned, _, baseline = diff_register(current, reg_state)
    reg_state["last_check"] = now
    print(f"Register: {len(current)} marks. Baseline run: {baseline}. "
          f"New: {len(new_marks)}, re-issued: {len(returned)}.")

    if baseline:
        send_ntfy(
            "New-rego watcher is live",
            f"Baseline recorded: {len(current)} current VH- marks. "
            f"New marks will be flagged from tomorrow's CASA refresh.",
            priority="low", tags="white_check_mark",
        )
    for mark in new_marks:
        info = current[mark]
        candidates[mark] = {
            "added": now,
            "desc": info["desc"],
            "icao_type": info["icao_type"],
            "first_registered": info["first_registered"],
            "hex": None,
            "last_hex_try": 0,
        }
        print(f"  NEW REGO: {mark} ({info['desc']}) first registered {info['first_registered']}")
    for mark in returned:
        print(f"  Re-issued mark (not a first): {mark}")


# ---------------------------------------------------------------------------
# ICAO24 lookups
# ---------------------------------------------------------------------------

def resolve_hex_for_mark(mark):
    try:
        resp = requests.get(HEXDB_REG_TO_HEX.format(reg=mark), headers=HEADERS, timeout=15)
        h = resp.text.strip().lower()
        if resp.status_code == 200 and re.fullmatch(r"[0-9a-f]{6}", h):
            return h
    except requests.RequestException:
        pass
    try:
        resp = requests.get(ADSBDB_AIRCRAFT.format(key=mark), headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            h = (resp.json().get("response", {}).get("aircraft", {}) or {}).get("mode_s")
            if h and re.fullmatch(r"[0-9a-fA-F]{6}", h):
                return h.lower()
    except (requests.RequestException, ValueError):
        pass
    return None


def lookup_hex(icao24):
    """-> ('found', mark) | ('not_found', None) | ('error', None)"""
    results = []
    try:
        resp = requests.get(HEXDB_AIRCRAFT.format(hex=icao24), headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            reg = (resp.json().get("Registration") or "").strip()
            if reg:
                return "found", normalize_mark(reg)
            results.append("not_found")
        elif resp.status_code == 404:
            results.append("not_found")
        else:
            results.append("error")
    except (requests.RequestException, ValueError):
        results.append("error")

    try:
        resp = requests.get(ADSBDB_AIRCRAFT.format(key=icao24), headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            reg = ((resp.json().get("response", {}) or {}).get("aircraft", {}) or {}).get("registration")
            if reg:
                return "found", normalize_mark(reg)
            results.append("not_found")
        elif resp.status_code == 404:
            results.append("not_found")
        else:
            results.append("error")
    except (requests.RequestException, ValueError):
        results.append("error")

    # Only call it "not found" if BOTH sources positively said so.
    return ("not_found", None) if results == ["not_found", "not_found"] else ("error", None)


# ---------------------------------------------------------------------------
# live traffic
# ---------------------------------------------------------------------------

def fetch_aircraft_in_range():
    dlat = RADIUS_KM / 111.0
    dlon = RADIUS_KM / (111.0 * math.cos(math.radians(CENTRE_LAT)))
    params = {
        "lamin": CENTRE_LAT - dlat, "lamax": CENTRE_LAT + dlat,
        "lomin": CENTRE_LON - dlon, "lomax": CENTRE_LON + dlon,
    }
    resp = requests.get(OPENSKY_STATES_URL, headers=HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    out = []
    for s in resp.json().get("states") or []:
        lat, lon = s[6], s[5]
        if lat is None or lon is None:
            continue
        dist = haversine_km(CENTRE_LAT, CENTRE_LON, lat, lon)
        if dist > RADIUS_KM:
            continue
        alt_m = s[13] if s[13] is not None else s[7]
        out.append({
            "icao24": (s[0] or "").strip().lower(),
            "callsign": (s[1] or "").strip(),
            "on_ground": bool(s[8]),
            "alt_ft": round(alt_m * 3.28084) if alt_m is not None else None,
            "dist_km": dist,
        })
    return out


def where_text(ac):
    if ac["on_ground"]:
        return f"on the ground {ac['dist_km']:.0f} km from {CENTRE_NAME} centre"
    alt = f" at {ac['alt_ft']:,} ft" if ac["alt_ft"] is not None else ""
    return f"airborne{alt}, {ac['dist_km']:.0f} km from {CENTRE_NAME} centre"


def alert_candidate(mark, cand, ac, notified, now):
    key = f"rego:{mark}"
    if now - notified.get(key, 0) < RENOTIFY_AFTER_HOURS * HOUR:
        return
    cs = f" - callsign {ac['callsign']}" if ac["callsign"] else ""
    first = f" (first registered {cand['first_registered']})" if cand.get("first_registered") else ""
    desc = f" {cand['desc']}" if cand.get("desc") else ""
    send_ntfy(
        f"NEW REGO near {CENTRE_NAME}: {mark}",
        f"{mark}{desc}{first}{cs} - {where_text(ac)}. Possible JetPhotos first rego.",
    )
    notified[key] = now


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    now = time.time()
    reg_state = load_json(REGISTER_STATE_FILE, {})
    candidates = load_json(CANDIDATES_FILE, {})
    notified = load_json(NOTIFIED_FILE, {})
    hex_checked = load_json(HEX_CHECKED_FILE, {})

    if TEST_REGISTRATION:
        mark = normalize_mark(TEST_REGISTRATION)
        candidates.setdefault(mark, {
            "added": now, "desc": "(TEST)", "icao_type": "", "first_registered": "",
            "hex": None, "last_hex_try": 0,
        })

    # drop very old candidates
    for m in [m for m, c in candidates.items() if now - c.get("added", now) > CANDIDATE_KEEP_DAYS * DAY]:
        del candidates[m]

    # resolve ICAO24 hex for candidates that don't have one yet
    for mark, cand in candidates.items():
        if cand.get("hex") or now - cand.get("last_hex_try", 0) < HEX_RETRY_HOURS * HOUR:
            continue
        cand["last_hex_try"] = now
        h = resolve_hex_for_mark(mark)
        if h:
            cand["hex"] = h
            print(f"Resolved {mark} -> {h}")
        else:
            print(f"{mark}: no hex in hexdb/adsbdb yet")
        time.sleep(0.2)

    # live scan
    try:
        aircraft = fetch_aircraft_in_range()
    except requests.RequestException as e:
        print(f"OpenSky fetch failed: {e}")
        aircraft = None

    if aircraft is not None:
        print(f"{len(aircraft)} aircraft within {RADIUS_KM} km of {CENTRE_NAME}.")
        cand_by_hex = {c["hex"]: m for m, c in candidates.items() if c.get("hex")}
        lookups = 0

        for ac in aircraft:
            h = ac["icao24"]

            if h in cand_by_hex:
                mark = cand_by_hex[h]
                alert_candidate(mark, candidates[mark], ac, notified, now)
                continue

            if not is_au_civil_hex(h):
                continue

            entry = hex_checked.get(h)
            ttl = FOUND_HEX_TTL_DAYS * DAY if entry and entry.get("reg") else NOT_FOUND_HEX_TTL_HOURS * HOUR
            if not entry or now - entry["ts"] > ttl:
                if lookups >= MAX_HEX_LOOKUPS_PER_RUN:
                    continue
                status, reg = lookup_hex(h)
                lookups += 1
                if status == "error":
                    continue  # transient - don't cache, don't alert
                entry = {"reg": reg, "ts": now, "alerted": (entry or {}).get("alerted", 0)}
                hex_checked[h] = entry
                time.sleep(0.2)

            if entry.get("reg"):
                mark = entry["reg"]
                if mark in candidates and not candidates[mark].get("hex"):
                    candidates[mark]["hex"] = h
                    alert_candidate(mark, candidates[mark], ac, notified, now)
                continue

            # Australian-range hex that no database can name
            if ALERT_UNIDENTIFIED and now - entry.get("alerted", 0) > UNIDENTIFIED_RENOTIFY_DAYS * DAY:
                cs = f" - callsign {ac['callsign']}" if ac["callsign"] else ""
                send_ntfy(
                    f"Unidentified VH aircraft near {CENTRE_NAME}",
                    f"ICAO24 {h.upper()}{cs} - {where_text(ac)}. Neither hexdb nor adsbdb knows "
                    f"this Australian hex - could be a brand-new registration.",
                    priority="low", tags="question",
                )
                entry["alerted"] = now

    # Daily CASA refresh runs last, so a slow/blocked casa.gov.au can never
    # delay live alerts. Any new candidates get picked up on the next run.
    maybe_refresh_register(reg_state, candidates, time.time())

    # housekeeping so state files stay small
    for h in [h for h, e in hex_checked.items() if now - e.get("ts", 0) > 60 * DAY]:
        del hex_checked[h]
    for k in [k for k, t in notified.items() if now - t > 60 * DAY]:
        del notified[k]

    save_json(REGISTER_STATE_FILE, reg_state)
    save_json(CANDIDATES_FILE, candidates)
    save_json(NOTIFIED_FILE, notified)
    save_json(HEX_CHECKED_FILE, hex_checked)


if __name__ == "__main__":
    main()

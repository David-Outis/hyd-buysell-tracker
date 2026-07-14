#!/usr/bin/env python3
"""
Hyderabad Buy/Sell Tracker
--------------------------
Polls r/HyderabadBuySell and r/HyderabadUsedItems (unauthenticated .json
endpoints), finds new mobile/laptop/tablet/console listings from today,
scores them, verifies they still exist before notifying (to avoid false
positives from deleted/glitched posts), detects reposts/edits, parses
Indian-style prices, self-heals from corrupt state, prunes old state,
writes report.md, logs run statistics, and pushes ntfy notifications.

Stdlib only. No dependencies to install.
"""

import json
import os
import re
import sys
import time
import hashlib
import random
import shutil
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

SUBREDDITS = [
    "HyderabadBuySell",
    "HyderabadUsedItems",
    "ChennaiBuyAndSell",
    "bangloremarketplace",     # note: "banglore" spelling (missing second "a")
    "BangaloreMarketplace",    # note: "bangalore" spelling - a different, separate subreddit
]

# Multiple fallback endpoints per subreddit (tried in order until one works).
# .json endpoints are tried first since they're structured and easy to parse.
# The .rss endpoint is included as a fallback since it's an older, simpler
# endpoint that has sometimes remained accessible when .json gets blocked.
ENDPOINT_TEMPLATES = [
    "https://www.reddit.com/r/{sub}/new.json?limit=100",
    "https://old.reddit.com/r/{sub}/new.json?limit=100",
    "https://reddit.com/r/{sub}/new.json?limit=100",
]

RSS_ENDPOINT_TEMPLATES = [
    "https://www.reddit.com/r/{sub}/new/.rss",
    "https://old.reddit.com/r/{sub}/new/.rss",
]

USER_AGENT = "HyderabadBuySellTracker/2.0 (by u/anonymous; contact via repo issues)"

SEEN_IDS_FILE = "seen_ids.json"
REPORT_FILE = "report.md"

ONLY_TODAY = True
MAX_AGE_HOURS = 24

# How long to keep an ID around in seen_ids.json after last activity (Part 14)
MEMORY_LIMIT_DAYS = 30

# How long a repost fingerprint stays valid (Part 9)
REPOST_WINDOW_DAYS = 7

# Minimum score for a listing to be reported (Part 11)
SCORE_THRESHOLD = 50

# Fast recheck: instead of waiting for the next scheduled (30-min) run to
# verify a brand-new post still exists, wait this many seconds within the
# same run and recheck immediately. Keeps false-positive protection while
# cutting notification latency drastically for items that sell fast.
FAST_RECHECK_DELAY_SECONDS = 45

# Network retry/backoff
MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 2

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else None

# ----------------------------------------------------------------------------
# Category / scoring rules
# ----------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    "Mobiles": [
        "iphone", "samsung", "galaxy", "oneplus", "redmi", "poco", "realme",
        "vivo", "oppo", "pixel", "nothing phone", "motorola", "moto ",
        "mobile", "smartphone",
    ],
    "Laptops": [
        "laptop", "macbook", "thinkpad", "ideapad", "notebook", "ultrabook",
        "dell xps", "hp pavilion", "asus vivobook", "acer aspire", "zenbook",
        "legion",
    ],
    "Tablets": [
        "ipad", "mi pad 6", "mi pad 7", "mi pad 8", "xiaomi pad 6",
        "xiaomi pad 7", "xiaomi pad 8",
    ],
    "Consoles": [
        "ps5", "ps4", "playstation", "xbox", "nintendo switch", "switch oled",
        "steam deck",
    ],
}

# Only these tablet models are allowed (per README: iPad 10th-gen+/Air/Pro/Mini, Mi Pad 6/7/8)
TABLET_ALLOWLIST_PATTERNS = [
    r"ipad\s*(10th|11th|12th)", r"ipad\s*air", r"ipad\s*pro", r"ipad\s*mini",
    r"mi\s*pad\s*[678]", r"xiaomi\s*pad\s*[678]",
]

CITY_HINTS = [
    # Hyderabad
    "hyderabad", "hyd", "secunderabad", "kukatpally", "gachibowli",
    "madhapur", "kondapur", "miyapur", "ameerpet", "dilsukhnagar",
    # Chennai
    "chennai", "madras", "tambaram", "velachery", "adyar", "anna nagar",
    "porur", "omr", "tnagar", "t nagar",
    # Bangalore
    "bangalore", "bengaluru", "blr", "koramangala", "indiranagar",
    "whitefield", "electronic city", "hsr layout", "marathahalli",
    "jayanagar",
]

GOOD_TITLE_HINTS = ["excellent condition", "like new", "sealed", "brand new",
                     "warranty", "bill available", "urgent sale"]

# Part 15 - Negative keywords: "want to buy" posts, not actual listings.
# If a title matches any of these, it's excluded entirely regardless of score.
WTB_PATTERNS = [
    r"\bwtb\b",
    r"\bwant(ed)?\s*to\s*buy\b",
    r"\bwanted\b",
    r"\blooking\s*for\b",
    r"\biso\b",
    r"\bin\s*search\s*of\b",
    r"\bneed\s+(a|an|any)\b",
    r"\bany(one)?\s+(selling|have)\b",
    r"\bwilling\s*to\s*buy\b",
    r"\bin\s*need\s*of\b",
]


def is_wtb(title):
    """Return True if the title looks like a 'want to buy' post rather than
    an actual for-sale listing."""
    text = title.lower()
    return any(re.search(p, text) for p in WTB_PATTERNS)

NOISE_PATTERNS = [
    r"\b\d{2,4}\s*gb\s*ram\b",
    r"\brtx\s*\d{3,4}\b",
    r"\bgtx\s*\d{3,4}\b",
    r"\b\d{2,4}\s*gb\b(?!\s*(storage|variant))",
]


# ----------------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------------

def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {msg}")


# ----------------------------------------------------------------------------
# Part 13 - Automatic recovery: state loading/saving
# ----------------------------------------------------------------------------

def load_state():
    """
    Load seen_ids.json. Self-heals if missing or corrupted.
    New format:
    {
        "<post_id>": {
            "state": "pending" | "notified",
            "first_seen": iso timestamp,
            "last_seen": iso timestamp,
            "fingerprint": "sha1...",
            "title": "...",
            "permalink": "..."
        },
        ...
    }
    """
    if not os.path.exists(SEEN_IDS_FILE):
        log(f"{SEEN_IDS_FILE} missing, creating fresh state.")
        return {}

    try:
        with open(SEEN_IDS_FILE, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return {}
        data = json.loads(raw)

        # Migrate from old list-based format if needed
        if isinstance(data, list):
            log("Old list-based seen_ids.json format detected, migrating.")
            migrated = {}
            now_iso = datetime.now(timezone.utc).isoformat()
            for pid in data:
                migrated[pid] = {
                    "state": "notified",
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "fingerprint": None,
                    "title": None,
                    "permalink": None,
                }
            return migrated

        if not isinstance(data, dict):
            raise ValueError("seen_ids.json is not a dict")
        return data

    except (json.JSONDecodeError, ValueError, OSError) as e:
        backup_name = f"{SEEN_IDS_FILE}.corrupt.{int(time.time())}.bak"
        try:
            shutil.copy(SEEN_IDS_FILE, backup_name)
            log(f"Corrupted {SEEN_IDS_FILE} ({e}); backed up to {backup_name}, starting fresh.")
        except OSError:
            log(f"Corrupted {SEEN_IDS_FILE} ({e}); could not back up, starting fresh.")
        return {}


def save_state(state):
    tmp_path = SEEN_IDS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp_path, SEEN_IDS_FILE)


def prune_state(state):
    """Part 14 - Memory limit: drop entries not seen in MEMORY_LIMIT_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=MEMORY_LIMIT_DAYS)
    pruned = {}
    removed = 0
    for pid, entry in state.items():
        last_seen_str = entry.get("last_seen") or entry.get("first_seen")
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
        except (TypeError, ValueError):
            last_seen = datetime.now(timezone.utc)
        if last_seen >= cutoff:
            pruned[pid] = entry
        else:
            removed += 1
    if removed:
        log(f"Pruned {removed} entries older than {MEMORY_LIMIT_DAYS} days.")
    return pruned


# ----------------------------------------------------------------------------
# Networking with retries (Part 13)
# ----------------------------------------------------------------------------

# Rate-limit (429) specific backoff config - deliberately longer/slower than
# the generic error backoff, since hammering Reddit while rate-limited risks
# a longer or harsher block.
RATE_LIMIT_BASE_SECONDS = 30
RATE_LIMIT_MAX_SECONDS = 90


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            return json.loads(raw)

        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Respect Retry-After if Reddit sends one, else use a longer
                # dedicated rate-limit backoff (not the generic curve).
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = RATE_LIMIT_BASE_SECONDS * attempt
                else:
                    wait = min(RATE_LIMIT_BASE_SECONDS * attempt, RATE_LIMIT_MAX_SECONDS)
                wait += random.uniform(0, 5)
                log(f"Rate limited (429) attempt {attempt}/{MAX_RETRIES}, "
                    f"backing off {wait:.1f}s")
                time.sleep(wait)
                continue

            if e.code == 403:
                # 403 means blocked/forbidden, not "try again shortly" like
                # 429. Retrying the *same* endpoint repeatedly almost never
                # helps here (it's usually an IP or User-Agent block), so
                # fail fast and let the caller move on to the next fallback
                # endpoint instead of burning minutes on backoff.
                log(f"HTTP 403 (forbidden/blocked) on this endpoint, "
                    f"not retrying it further.")
                return None

            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log(f"HTTP error ({e.code}) attempt {attempt}/{MAX_RETRIES}, retrying in {wait:.1f}s")
            time.sleep(wait)

        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError, ConnectionError) as e:
            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log(f"Fetch failed ({e}) attempt {attempt}/{MAX_RETRIES}, retrying in {wait:.1f}s")
            time.sleep(wait)
    return None


def fetch_raw(url):
    """Like fetch_json but returns raw bytes instead of parsed JSON, with the
    same retry/backoff/403-fail-fast behavior. Used for the RSS fallback."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()

        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = RATE_LIMIT_BASE_SECONDS * attempt
                else:
                    wait = min(RATE_LIMIT_BASE_SECONDS * attempt, RATE_LIMIT_MAX_SECONDS)
                wait += random.uniform(0, 5)
                log(f"Rate limited (429) attempt {attempt}/{MAX_RETRIES}, "
                    f"backing off {wait:.1f}s")
                time.sleep(wait)
                continue

            if e.code == 403:
                log(f"HTTP 403 (forbidden/blocked) on this endpoint, "
                    f"not retrying it further.")
                return None

            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log(f"HTTP error ({e.code}) attempt {attempt}/{MAX_RETRIES}, retrying in {wait:.1f}s")
            time.sleep(wait)

        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log(f"Fetch failed ({e}) attempt {attempt}/{MAX_RETRIES}, retrying in {wait:.1f}s")
            time.sleep(wait)
    return None


ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _html_to_text(html_str):
    """Strip HTML tags down to plain text for use in notifications."""
    import html as html_module
    if not html_str:
        return ""
    # Reddit's RSS content field also includes a "submitted by / comments"
    # footer link block we don't want in the notification body.
    text = re.sub(r"<a[^>]*>\s*\[link\]\s*</a>|<a[^>]*>\s*\[comments\]\s*</a>",
                  "", html_str, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_module.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_rss_feed(raw_bytes):
    """
    Parse Reddit's Atom-format RSS feed into the same shape the rest of the
    script expects from the .json endpoint: a list of {"data": {...}} dicts
    with id, title, created_utc, permalink, selftext.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError as e:
        log(f"Failed to parse RSS/Atom feed: {e}")
        return []

    posts = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        try:
            entry_id_raw = entry.findtext(f"{ATOM_NS}id", default="")
            # Reddit's Atom id looks like: t3_abc123
            m = re.search(r"t3_([a-z0-9]+)", entry_id_raw)
            post_id = m.group(1) if m else entry_id_raw

            title = (entry.findtext(f"{ATOM_NS}title", default="") or "").strip()

            link_el = entry.find(f"{ATOM_NS}link")
            permalink = link_el.get("href") if link_el is not None else ""

            content_raw = entry.findtext(f"{ATOM_NS}content", default="")
            selftext = _html_to_text(content_raw)

            time_str = (entry.findtext(f"{ATOM_NS}published")
                        or entry.findtext(f"{ATOM_NS}updated"))
            if time_str:
                # e.g. 2026-07-14T18:40:00+00:00
                dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                created_utc = dt.timestamp()
            else:
                created_utc = datetime.now(timezone.utc).timestamp()

            if not post_id or not title:
                continue

            posts.append({
                "data": {
                    "id": post_id,
                    "title": title,
                    "selftext": selftext,
                    "created_utc": created_utc,
                    "permalink": permalink.replace("https://reddit.com", "")
                                            .replace("https://www.reddit.com", "")
                                            .replace("https://old.reddit.com", ""),
                }
            })
        except Exception as e:
            log(f"Skipping malformed RSS entry: {e}")
            continue

    return posts


def fetch_subreddit_posts(sub):
    """Try each .json fallback endpoint first, then fall back to the RSS/Atom
    feed if every .json endpoint fails (e.g. all blocked with 403)."""
    for template in ENDPOINT_TEMPLATES:
        url = template.format(sub=sub)
        data = fetch_json(url)
        if data:
            try:
                return data["data"]["children"]
            except (KeyError, TypeError):
                continue

    log(f"All .json endpoints failed for r/{sub}; trying RSS fallback.")
    for template in RSS_ENDPOINT_TEMPLATES:
        url = template.format(sub=sub)
        raw = fetch_raw(url)
        if raw:
            posts = parse_rss_feed(raw)
            if posts:
                log(f"RSS fallback succeeded for r/{sub} ({len(posts)} entries).")
                return posts

    log(f"All endpoints (json + rss) failed for r/{sub}; skipping this subreddit for this run.")
    return []


def send_ntfy(title, message):
    if not NTFY_URL:
        return
    try:
        req = urllib.request.Request(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "default",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        # Part 13: ntfy failing must never stop the run
        log(f"ntfy notification failed: {e}")


# ----------------------------------------------------------------------------
# Part 10 - Indian price parsing
# ----------------------------------------------------------------------------

PRICE_PATTERNS = [
    # ₹23,500  or  ₹ 23500
    r"₹\s*([\d,]+(?:\.\d+)?)",
    # Rs.23500 / Rs 23,000 / rs. 23000
    r"\brs\.?\s*([\d,]+(?:\.\d+)?)",
    # 23500/-
    r"\b([\d,]+)\s*/-",
    # 23k final / 23 k / 23.5k / 30K
    r"\b(\d+(?:\.\d+)?)\s*k\b",
    # 25 thousand
    r"\b(\d+(?:\.\d+)?)\s*thousand\b",
    # plain number with 4-6 digits followed by "negotiable"/"fixed"/"final"
    r"\b([\d,]{4,7})\s*(?:negotiable|fixed|final|only)\b",
]

NOISE_CONTEXT_PATTERNS = [
    r"\d{2,4}\s*gb\s*ram",
    r"\d{2,4}\s*gb(?!\s*(variant|storage))",
    r"rtx\s*\d{3,4}",
    r"gtx\s*\d{3,4}",
    r"iphone\s*\d{1,2}",
]


def _strip_noise(text):
    cleaned = text
    for pat in NOISE_CONTEXT_PATTERNS:
        cleaned = re.sub(pat, " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def extract_price(title):
    """
    Returns an int price in rupees, or None.
    Handles: ₹23,500 | Rs.23500 | 23500/- | 23k | 23 K | 23.5k |
             25 thousand | 35 negotiable | 25,500 fixed | Rs 23000 | 23k final
    Ignores: 128GB, 16GB RAM, RTX 3060, iPhone 15 (plain model numbers).
    """
    text = title.lower()
    cleaned = _strip_noise(text)

    for pat in PRICE_PATTERNS:
        m = re.search(pat, cleaned, flags=re.IGNORECASE)
        if not m:
            continue
        raw_num = m.group(1).replace(",", "")
        try:
            value = float(raw_num)
        except ValueError:
            continue

        if "k" in pat or "thousand" in pat:
            value *= 1000

        value = int(round(value))

        # Sanity check: ignore absurd values (likely false positive)
        if 500 <= value <= 500000:
            return value

    return None


# ----------------------------------------------------------------------------
# Part 9 - Repost / edit fingerprinting
# ----------------------------------------------------------------------------

def make_fingerprint(title, price):
    basis = f"{title.lower().strip()}|{price if price is not None else ''}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def find_repost(fingerprint, state, current_id):
    """Look for another (different) id with the same fingerprint within the
    repost window. Returns the old id if found, else None."""
    if not fingerprint:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=REPOST_WINDOW_DAYS)
    for pid, entry in state.items():
        if pid == current_id:
            continue
        if entry.get("fingerprint") != fingerprint:
            continue
        last_seen_str = entry.get("last_seen") or entry.get("first_seen")
        try:
            last_seen = datetime.fromisoformat(last_seen_str)
        except (TypeError, ValueError):
            continue
        if last_seen >= cutoff:
            return pid
    return None


# ----------------------------------------------------------------------------
# Categorization + scoring (Part 11)
# ----------------------------------------------------------------------------

def categorize(title):
    text = title.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                if category == "Tablets":
                    if not any(re.search(p, text) for p in TABLET_ALLOWLIST_PATTERNS):
                        continue
                return category
    return None


def score_listing(title, category, price):
    text = title.lower()
    score = 0
    reasons = []

    category_points = {"Mobiles": 40, "Laptops": 40, "Tablets": 40, "Consoles": 35}
    if category in category_points:
        score += category_points[category]
        reasons.append(f"+{category_points[category]} {category}")

    if price is not None:
        score += 30
        reasons.append("+30 Price found")

    if any(hint in text for hint in HYDERABAD_HINTS):
        score += 20
        reasons.append("+20 Hyderabad")

    if any(hint in text for hint in GOOD_TITLE_HINTS):
        score += 15
        reasons.append("+15 Excellent title")

    return score, reasons


# ----------------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------------

def is_today_utc(created_utc):
    post_dt = datetime.fromtimestamp(created_utc, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    if ONLY_TODAY:
        return post_dt.date() == now.date()
    return (now - post_dt) <= timedelta(hours=MAX_AGE_HOURS)


# ----------------------------------------------------------------------------
# Main run
# ----------------------------------------------------------------------------

def main():
    run_start = time.time()
    started_at = datetime.now(timezone.utc)
    state = load_state()
    now_iso = started_at.isoformat()

    report_sections = {cat: [] for cat in ["Mobiles", "Laptops", "Tablets", "Consoles"]}
    to_notify = []  # (title, permalink, category, price, score, selftext)
    new_pending = []  # freshly-seen posts this run, awaiting fast recheck

    total_fetched = 0
    total_matched = 0
    total_new = 0
    total_updated = 0
    total_skipped = 0

    for sub in SUBREDDITS:
        posts = fetch_subreddit_posts(sub)
        total_fetched += len(posts)

        # Small pause between subreddits to reduce the chance of tripping
        # a rate limit back-to-back, since .rss is now the primary working
        # path (not just an occasional fallback) after .json got blocked.
        if sub != SUBREDDITS[-1]:
            time.sleep(3)

        for post in posts:
            try:
                p = post["data"]
                post_id = p["id"]
                title = p.get("title", "").strip()
                selftext = (p.get("selftext") or "").strip()
                created_utc = p.get("created_utc", 0)
                permalink = "https://reddit.com" + p.get("permalink", "")
            except (KeyError, TypeError):
                total_skipped += 1
                continue

            if not is_today_utc(created_utc):
                # Still record it so it's never reconsidered, but don't report.
                if post_id not in state:
                    state[post_id] = {
                        "state": "notified",
                        "first_seen": now_iso,
                        "last_seen": now_iso,
                        "fingerprint": None,
                        "title": title,
                        "permalink": permalink,
                    }
                total_skipped += 1
                continue

            if is_wtb(title):
                total_skipped += 1
                continue

            category = categorize(title)
            if not category:
                total_skipped += 1
                continue

            price = extract_price(title)

            score, _reasons = score_listing(title, category, price)
            if score < SCORE_THRESHOLD:
                total_skipped += 1
                continue

            total_matched += 1
            fingerprint = make_fingerprint(title, price)

            existing = state.get(post_id)

            if existing is None:
                # Check repost against fingerprint history first
                repost_of = find_repost(fingerprint, state, post_id)
                state[post_id] = {
                    "state": "pending",
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "fingerprint": fingerprint,
                    "title": title,
                    "permalink": permalink,
                    "selftext": selftext,
                }
                if repost_of:
                    # Treat as repost: skip notifying again, just track it.
                    state[post_id]["state"] = "notified"
                    total_updated += 1
                else:
                    total_new += 1
                    new_pending.append({
                        "id": post_id,
                        "title": title,
                        "permalink": permalink,
                        "category": category,
                        "price": price,
                        "score": score,
                        "selftext": selftext,
                    })
                # New posts are never notified same-run (yet) - they go
                # through the fast recheck below before falling back to
                # waiting for the next scheduled run.
                continue

            # Post already known.
            existing["last_seen"] = now_iso
            existing["title"] = title
            existing["permalink"] = permalink
            if selftext:
                existing["selftext"] = selftext

            if existing.get("state") == "pending":
                # Verified still exists on this second run -> notify now.
                existing["state"] = "notified"
                existing["fingerprint"] = fingerprint
                to_notify.append((title, permalink, category, price, score,
                                   selftext or existing.get("selftext", "")))
                total_updated += 1
            else:
                # Already notified before; nothing to do.
                total_skipped += 1

    # ------------------------------------------------------------------
    # Fast recheck (shortened verification window)
    # ------------------------------------------------------------------
    # Waiting a full 30-min schedule cycle to verify a post still exists
    # is too slow for items that can sell within minutes. Instead, after
    # a short pause, we refetch and check the same run: if a "new" post
    # is still present, notify immediately. If it disappeared (deleted/
    # glitch), it's left in "pending" state and simply never promoted -
    # protecting against the false positives Part 8 was designed to avoid.
    if new_pending:
        log(f"Fast recheck: waiting {FAST_RECHECK_DELAY_SECONDS}s before "
            f"re-verifying {len(new_pending)} new listing(s)...")
        time.sleep(FAST_RECHECK_DELAY_SECONDS)

        still_present_ids = set()
        for sub in SUBREDDITS:
            recheck_posts = fetch_subreddit_posts(sub)
            for post in recheck_posts:
                try:
                    still_present_ids.add(post["data"]["id"])
                except (KeyError, TypeError):
                    continue

        for item in new_pending:
            if item["id"] in still_present_ids:
                state[item["id"]]["state"] = "notified"
                state[item["id"]]["last_seen"] = now_iso
                to_notify.append((
                    item["title"], item["permalink"], item["category"],
                    item["price"], item["score"], item["selftext"],
                ))
                total_updated += 1
                total_new -= 1  # it's now counted as updated/notified, not just "new"
            else:
                log(f"Post {item['id']} disappeared before recheck; "
                    f"leaving as pending for next scheduled run.")

    # Build report + notifications from verified (to_notify) listings.
    for title, permalink, category, price, score, selftext in to_notify:
        price_str = f"Rs.{price:,}" if price is not None else "Price not found"
        line = f"- [{title}]({permalink}) — {price_str} (score {score})"
        report_sections.setdefault(category, []).append(line)

    # ntfy notifications (include post body alongside title)
    for title, permalink, category, price, score, selftext in to_notify:
        price_str = f"Rs.{price:,}" if price is not None else "price n/a"
        body_preview = selftext[:400] if selftext else "(no post body)"
        message = f"{price_str}\n{permalink}\n\n{body_preview}"
        send_ntfy(f"New {category}: {title[:60]}", message)

    # Prune + save state (Part 14)
    state = prune_state(state)
    save_state(state)

    # Write report.md
    write_report(report_sections)

    elapsed = time.time() - run_start

    # Part 12 - structured logging
    log("=" * 48)
    log(f"Started: {started_at.strftime('%Y-%m-%d %H:%M')}")
    for sub in SUBREDDITS:
        log(sub)
    log(f"Fetched: {total_fetched}")
    log(f"Matched: {total_matched}")
    log(f"New: {total_new}")
    log(f"Updated (notified): {total_updated}")
    log(f"Skipped: {total_skipped}")
    log(f"Time: {elapsed:.1f} sec")
    log("=" * 48)


def write_report(report_sections):
    now = datetime.now(timezone.utc)
    lines = [f"# Hyderabad Buy/Sell Report - {now.strftime('%Y-%m-%d %H:%M')} UTC\n"]

    section_titles = {
        "Mobiles": "## Mobiles",
        "Laptops": "## Laptops",
        "Tablets": "## Tablets (iPad 10th-gen+/Air/Pro/Mini, Mi Pad 6/7/8 only)",
        "Consoles": "## Game Consoles",
    }
    empty_msgs = {
        "Mobiles": "_No new mobile listings found in this run._",
        "Laptops": "_No new laptop listings found in this run._",
        "Tablets": "_No new matching tablet listings found in this run._",
        "Consoles": "_No new game console listings found in this run._",
    }

    for category in ["Mobiles", "Laptops", "Tablets", "Consoles"]:
        lines.append(section_titles[category])
        lines.append("")
        entries = report_sections.get(category, [])
        if entries:
            lines.extend(entries)
        else:
            lines.append(empty_msgs[category])
        lines.append("")
        lines.append("")

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Last-resort catch so GitHub Actions logs the failure clearly
        # instead of a bare traceback with no context.
        log(f"FATAL: unhandled error: {e}")
        raise

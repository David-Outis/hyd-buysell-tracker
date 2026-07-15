#!/usr/bin/env python3
"""
Hyderabad Buy/Sell Tracker
--------------------------
Polls r/HyderabadBuySell, r/HyderabadUsedItems, r/ChennaiBuyAndSell,
r/bangloremarketplace, and r/BangaloreMarketplace, finds new mobile/laptop/
tablet/console listings from today, scores them, verifies they still exist
before notifying (to avoid false positives from deleted/glitched posts),
detects reposts/edits, parses Indian-style prices, self-heals from corrupt
state, prunes old state, writes report.md, logs run statistics, and pushes
ntfy notifications (including the post body).

Fetch strategy: Reddit's unauthenticated .json endpoints are confirmed
blocked (403) as of July 2026, so this fetches from the RSS/Atom feed
(.rss) by default. See TRY_JSON_ENDPOINTS below to re-enable .json if
Reddit ever unblocks it.

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

# Network retry/backoff
# Earlier fallback endpoints fail fast (1 attempt) since www.reddit.com is
# reliably blocked and there's always another endpoint to try next. The
# *last* fallback endpoint gets more attempts since there's nowhere further
# to fall back to if it also gets rate-limited.
MAX_RETRIES = 1
LAST_FALLBACK_MAX_RETRIES = 3
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
    "PCs": [
        # "pc" / "cpu" are dangerous as plain substrings (they'd match inside
        # "upcoming", "PCX" scooter, "occupied", etc.), so these are matched
        # with word-boundary regex instead of the loose substring check used
        # for the other categories above. See categorize() below.
        r"regex:\bpc\b", r"regex:\bcpu\b", r"regex:\bdesktop\b",
        r"regex:\bgaming\s*pc\b", r"regex:\bcustom\s*pc\b",
        r"regex:\bprebuilt\b", r"regex:\bpre-built\b",
        r"regex:\bmotherboard\b", r"regex:\bcabinet\b", r"regex:\bsmps\b",
        r"regex:\bfull\s*tower\b", r"regex:\bmid\s*tower\b",
        r"regex:\bryzen\s*[3579]\b", r"regex:\bcore\s*i[3579]\b",
        r"regex:\bgraphics\s*card\b",
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


# Part 16 - Category exclusions: movie tickets and watches/smartwatches are
# not desired categories (only Mobiles/Laptops/Tablets/Consoles), but they
# can slip through via keyword collisions - e.g. "galaxy" (meant for Samsung
# Galaxy phones) also matches "Galaxy Watch", and some cinema/venue names
# can coincidentally match a category keyword. Exclude them outright,
# regardless of score, before categorization ever runs.
EXCLUDE_PATTERNS = [
    r"\bticket(s)?\b",
    r"\bimax\b",
    r"\bmovie\b",
    r"\b(pvr|inox|cinepolis|multiplex|cinema)\b",
    r"\bshow\b",
    r"\bwatch(es)?\b",
    r"\bsmart\s*watch\b",
]


def is_excluded(title):
    """Return True if the title matches an excluded category (movie
    tickets, watches, etc.) that should never be reported even if it
    otherwise matches a category keyword like 'galaxy'."""
    text = title.lower()
    return any(re.search(p, text) for p in EXCLUDE_PATTERNS)


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
RATE_LIMIT_BASE_SECONDS = 20
RATE_LIMIT_MAX_SECONDS = 60


def fetch_json(url, max_retries=None):
    if max_retries is None:
        max_retries = MAX_RETRIES
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            return json.loads(raw)

        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt >= max_retries:
                    # No point sleeping if this was the last attempt on this
                    # endpoint - fail now and let the caller fall back to the
                    # next endpoint immediately.
                    log(f"Rate limited (429), no attempts left on this "
                        f"endpoint, failing fast to try the next fallback.")
                    return None
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
                log(f"Rate limited (429) attempt {attempt}/{max_retries}, "
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
            log(f"HTTP error ({e.code}) attempt {attempt}/{max_retries}, retrying in {wait:.1f}s")
            time.sleep(wait)

        except (urllib.error.URLError, TimeoutError,
                json.JSONDecodeError, ConnectionError) as e:
            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log(f"Fetch failed ({e}) attempt {attempt}/{max_retries}, retrying in {wait:.1f}s")
            time.sleep(wait)
    return None


def fetch_raw(url, max_retries=None):
    """Like fetch_json but returns raw bytes instead of parsed JSON, with the
    same retry/backoff/403-fail-fast behavior. Used for the RSS fallback."""
    if max_retries is None:
        max_retries = MAX_RETRIES
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()

        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt >= max_retries:
                    log(f"Rate limited (429), no attempts left on this "
                        f"endpoint, failing fast to try the next fallback.")
                    return None
                retry_after = e.headers.get("Retry-After") if e.headers else None
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        wait = RATE_LIMIT_BASE_SECONDS * attempt
                else:
                    wait = min(RATE_LIMIT_BASE_SECONDS * attempt, RATE_LIMIT_MAX_SECONDS)
                wait += random.uniform(0, 5)
                log(f"Rate limited (429) attempt {attempt}/{max_retries}, "
                    f"backing off {wait:.1f}s")
                time.sleep(wait)
                continue

            if e.code == 403:
                log(f"HTTP 403 (forbidden/blocked) on this endpoint, "
                    f"not retrying it further.")
                return None

            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 1)
            log(f"HTTP error ({e.code}) attempt {attempt}/{max_retries}, retrying in {wait:.1f}s")
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


# .json has been confirmed permanently blocked (403) as of July 2026.
# Trying it every run wastes 3 requests/subreddit for nothing and adds no
# value - flip this to True only if you want to periodically re-test
# whether Reddit has unblocked it (e.g. manually, once in a while).
TRY_JSON_ENDPOINTS = False


def fetch_subreddit_posts(sub):
    """Try each .json fallback endpoint first (if enabled), then fall back
    to the RSS/Atom feed. .json is disabled by default since it's confirmed
    blocked - skipping it cuts request volume roughly in half/third."""
    if TRY_JSON_ENDPOINTS:
        for template in ENDPOINT_TEMPLATES:
            url = template.format(sub=sub)
            data = fetch_json(url)
            if data:
                try:
                    return data["data"]["children"]
                except (KeyError, TypeError):
                    continue
        log(f"All .json endpoints failed for r/{sub}; trying RSS fallback.")

    for i, template in enumerate(RSS_ENDPOINT_TEMPLATES):
        url = template.format(sub=sub)
        is_last = (i == len(RSS_ENDPOINT_TEMPLATES) - 1)
        # Fail fast (1 attempt) on earlier endpoints and move to the next
        # fallback quickly. On the *last* fallback there's nowhere else to
        # go, so give it a real retry instead of giving up on the subreddit
        # entirely after a single 429.
        retries = LAST_FALLBACK_MAX_RETRIES if is_last else MAX_RETRIES
        raw = fetch_raw(url, max_retries=retries)
        if raw:
            posts = parse_rss_feed(raw)
            if posts:
                log(f"RSS fallback succeeded for r/{sub} ({len(posts)} entries).")
                return posts

    log(f"All endpoints failed for r/{sub}; skipping this subreddit for this run.")
    return []


NTFY_MAX_RETRIES = 2
NTFY_RETRY_BACKOFF_SECONDS = 5


def _ascii_safe_title(title):
    """HTTP headers must be Latin-1 encodable. Titles can contain characters
    like en-dashes (\u2013), curly quotes, etc. that crash urllib if sent
    raw in a header. Strip/replace anything outside Latin-1 rather than
    letting the whole notification fail."""
    return title.encode("latin-1", errors="replace").decode("latin-1")


def send_ntfy(title, message):
    if not NTFY_URL:
        return

    safe_title = _ascii_safe_title(title)

    for attempt in range(1, NTFY_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                NTFY_URL,
                data=message.encode("utf-8"),
                headers={
                    "Title": safe_title,
                    "Priority": "default",
                    "User-Agent": USER_AGENT,
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            return
        except Exception as e:
            # Network is unreachable / transient errors get one quick retry;
            # anything still failing after that must never stop the run
            # (Part 13).
            if attempt < NTFY_MAX_RETRIES:
                log(f"ntfy notification failed ({e}), retrying "
                    f"({attempt}/{NTFY_MAX_RETRIES})...")
                time.sleep(NTFY_RETRY_BACKOFF_SECONDS)
            else:
                log(f"ntfy notification failed: {e}")


# ----------------------------------------------------------------------------
# Part 10 - Indian price parsing
# ----------------------------------------------------------------------------

PRICE_PATTERNS = [
    # ₹23,500  or  ₹ 23500
    r"₹\s*([\d,]+(?:\.\d+)?)",
    # 73500₹  or  73,500 ₹  (symbol trailing the number - Part 18 fix;
    # previously only leading-₹ was matched, so titles like
    # "...phone 73500₹" scored no price at all)
    r"\b([\d,]+(?:\.\d+)?)\s*₹",
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
            # Keywords prefixed "regex:" are matched with word-boundary
            # regex instead of a loose substring check (used for short,
            # collision-prone tokens like "pc"/"cpu" - see PCs category).
            if kw.startswith("regex:"):
                matched = bool(re.search(kw[len("regex:"):], text))
            else:
                matched = kw in text

            if matched:
                if category == "Tablets":
                    if not any(re.search(p, text) for p in TABLET_ALLOWLIST_PATTERNS):
                        continue
                return category
    return None


def score_listing(title, category, price, selftext=""):
    """Score a listing. `title` drives the "Excellent title" phrase check
    (kept title-only on purpose - that's specifically about how the listing
    is *titled*), while price and city-match are checked against the
    combined title+body text, since sellers very often put the price and
    location only in the post body rather than the title (Part 17 fix -
    previously price/city were title-only and this silently dropped a lot
    of well-formed listings, e.g. genuine laptop posts with no price/city
    in the title, under SCORE_THRESHOLD)."""
    title_text = title.lower()
    full_text = f"{title} {selftext}".lower()
    score = 0
    reasons = []

    category_points = {"Mobiles": 40, "Laptops": 40, "Tablets": 40, "Consoles": 35, "PCs": 40}
    if category in category_points:
        score += category_points[category]
        reasons.append(f"+{category_points[category]} {category}")

    if price is not None:
        score += 30
        reasons.append("+30 Price found")

    if any(hint in full_text for hint in CITY_HINTS):
        score += 20
        reasons.append("+20 City match")

    if any(hint in title_text for hint in GOOD_TITLE_HINTS):
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

    report_sections = {cat: [] for cat in ["Mobiles", "Laptops", "Tablets", "Consoles", "PCs"]}
    to_notify = []  # (title, permalink, category, price, score, selftext)

    total_fetched = 0
    total_matched = 0
    total_new = 0
    total_updated = 0
    total_skipped = 0

    for sub in SUBREDDITS:
        posts = fetch_subreddit_posts(sub)
        total_fetched += len(posts)

        # Pause between subreddits to reduce the chance of tripping a rate
        # limit in the first place, since .rss is now the primary working
        # path (not just an occasional fallback) after .json got blocked.
        if sub != SUBREDDITS[-1]:
            time.sleep(45)

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

            if is_excluded(title):
                total_skipped += 1
                continue

            category = categorize(title)
            if not category:
                total_skipped += 1
                continue

            # Part 17 - price is frequently only in the post body, not the
            # title (e.g. "Selling my HP Pavilion laptop" with "Price:
            # Rs.25000" down in selftext), so check the title first and
            # fall back to the body if nothing was found there.
            price = extract_price(title)
            if price is None and selftext:
                price = extract_price(selftext)

            score, _reasons = score_listing(title, category, price, selftext)
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
                    "state": "notified",
                    "first_seen": now_iso,
                    "last_seen": now_iso,
                    "fingerprint": fingerprint,
                    "title": title,
                    "permalink": permalink,
                    "selftext": selftext,
                }
                if repost_of:
                    # Treat as repost: skip notifying again, just track it.
                    total_updated += 1
                else:
                    total_new += 1
                    to_notify.append((title, permalink, category, price, score, selftext))
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
        "PCs": "## Desktop PCs",
    }
    empty_msgs = {
        "Mobiles": "_No new mobile listings found in this run._",
        "Laptops": "_No new laptop listings found in this run._",
        "Tablets": "_No new matching tablet listings found in this run._",
        "Consoles": "_No new game console listings found in this run._",
        "PCs": "_No new desktop PC listings found in this run._",
    }

    for category in ["Mobiles", "Laptops", "Tablets", "Consoles", "PCs"]:
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

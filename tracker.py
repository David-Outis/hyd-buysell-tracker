#!/usr/bin/env python3
"""
Hyderabad Buy/Sell Tracker
--------------------------
Polls r/HyderabadBuySell and r/HyderabadUsedItems using Reddit's public,
unauthenticated JSON endpoints (no API key needed). Classifies fresh posts
into:

  1. Mobiles        - any brand/model, any price, price flagged if missing
  2. Laptops        - only <= LAPTOP_MAX_PRICE, or flagged "needs follow-up"
                      if no price is mentioned
  3. Tablets        - only iPad 10th-gen+/Air/Pro/Mini(newer) and
                      Xiaomi Mi Pad 6/7/8
  4. Game Consoles  - PS4/PS5, Xbox One/Series S/X, Nintendo Switch/Switch 2,
                      Steam Deck - any price, price flagged if missing

Dedup state (seen post IDs) is stored in seen_ids.json and committed back to
the repo by the GitHub Actions workflow so re-runs only report NEW posts.

ONLY-TODAY FILTER:
Even if a post is "new" (not yet in seen_ids.json), it is skipped if it was
created before today (UTC). This prevents old backlog posts from surfacing
just because they hadn't been seen yet (e.g. the very first run, or a gap
in scheduling). Controlled by ONLY_TODAY / MAX_AGE_HOURS below.

NOTE ON RELIABILITY:
This uses the unauthenticated old.reddit.com/.json endpoints. Reddit can
rate-limit or block requests that look automated. Keep the polling
interval reasonable (every 30-60 min) and always send a descriptive
User-Agent. If/when official Reddit API (OAuth) access is approved, swap
`fetch_new_posts()` to use PRAW instead - the rest of the pipeline
(classification, dedup, reporting) does not need to change.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

SUBREDDITS = ["HyderabadBuySell", "HyderabadUsedItems"]
POSTS_PER_SUB = 50          # how many recent posts to pull per sub, per sort
SORTS = ["new", "hot"]      # per your notes: check both, "new" gets buried
LAPTOP_MAX_PRICE = 30000    # rupees

# --- Only-today filter -----------------------------------------------------
# If True, any post created before "today" (UTC calendar day) is skipped,
# regardless of whether it's already in seen_ids.json. This is what keeps
# the tracker from reporting yesterday's (or older) listings.
ONLY_TODAY = True
# Belt-and-suspenders cap: also skip anything older than this many hours,
# in case a post is timestamped just after midnight UTC but is effectively
# an old listing that got bumped. Set to None to disable and rely purely
# on the calendar-day check above.
MAX_AGE_HOURS = 24

SEEN_IDS_FILE = os.path.join(os.path.dirname(__file__), "seen_ids.json")
REPORT_FILE = os.path.join(os.path.dirname(__file__), "report.md")
USER_AGENT = "python:hyd-buysell-tracker:v1.0 (by /u/jgoja)"

# NTFY_TOPIC must be provided via environment / GitHub Actions secret.
# If it's not set, push notifications are simply skipped (report.md/stdout
# still work fine).
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else ""

# --------------------------------------------------------------------------
# CLASSIFICATION RULES
# --------------------------------------------------------------------------

MOBILE_KEYWORDS = [
    "iphone", "samsung", "galaxy", "oneplus", "one plus", "redmi", "mi note",
    "poco", "realme", "vivo", "oppo", "pixel", "nothing phone", "motorola",
    "moto g", "moto edge", "asus rog phone", "iqoo", "infinix", "lava",
    "mobile phone", "smartphone",
]

LAPTOP_KEYWORDS = [
    "laptop", "macbook", "thinkpad", "ideapad", "pavilion", "inspiron",
    "vostro", "latitude", "zenbook", "vivobook", "legion", "predator",
    "aspire", "chromebook", "notebook pc", "gaming laptop", "ultrabook",
]

# Only these tablet models count - everything else is ignored per spec
TABLET_PATTERNS = [
    re.compile(r"\bipad\s*(10th|11th|air|pro|mini)\b", re.I),
    re.compile(r"\bipad\s*(gen(eration)?\s*1[01])\b", re.I),
    re.compile(r"\bmi\s*pad\s*[678]\b", re.I),
    re.compile(r"\bmipad\s*[678]\b", re.I),
    re.compile(r"\bxiaomi\s*pad\s*[678]\b", re.I),
]
# Explicitly excluded / ignored tablet mentions (older iPad, other brands)
TABLET_EXCLUDE_PATTERNS = [
    re.compile(r"\bipad\s*(1st|2nd|3rd|4th|5th|6th|7th|8th|9th)\b", re.I),
    re.compile(r"\bmi\s*pad\s*[1-5]\b", re.I),
]

CONSOLE_KEYWORDS = [
    "ps4", "ps5", "playstation 4", "playstation 5", "playstation5",
    "playstation4", "xbox one", "xbox series", "xbox 360",
    "nintendo switch", "switch 2", "steam deck", "steamdeck",
]

PRICE_PATTERNS = [
    re.compile(r"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)\s*(k)?", re.I),
    re.compile(r"\b([\d,]{3,7})\s*(?:rs|rupees|/-)\b", re.I),
    re.compile(r"\bprice\s*[:\-]?\s*([\d,]{3,7})\b", re.I),
    re.compile(r"\basking\s*(?:price)?\s*[:\-]?\s*([\d,]{3,7})\b", re.I),
    re.compile(r"\b(\d{1,3})\s*k\b", re.I),  # e.g. "15k"
]

# Text patterns that indicate the listing is a tablet, not a phone - used to
# suppress false-positive "mobile" matches from brand names like "Samsung"
# appearing in "Samsung Galaxy Tab" or similar tablet-only listings.
TABLET_INDICATOR_PATTERNS = [
    re.compile(r"\bipad\b", re.I),
    re.compile(r"\bgalaxy\s*tab\b", re.I),
    re.compile(r"\bmi\s*pad\b", re.I),
    re.compile(r"\bmipad\b", re.I),
    re.compile(r"\btablet\b", re.I),
]


def extract_price(text):
    """Best-effort price extraction from title+body text. Returns int rupees or None."""
    if not text:
        return None
    for pattern in PRICE_PATTERNS:
        m = pattern.search(text)
        if m:
            num = m.group(1).replace(",", "")
            try:
                value = float(num)
            except ValueError:
                continue
            # handle "k" suffix -> thousands
            if len(m.groups()) > 1 and m.group(2) and m.group(2).lower() == "k":
                value *= 1000
            elif value < 1000 and "k" in text[max(0, m.start() - 2):m.end() + 2].lower():
                value *= 1000
            return int(value)
    return None


def classify_post(title, body):
    """Return a dict of category -> bool for a given post's combined text."""
    text = f"{title} {body}".lower()

    looks_like_tablet_listing = any(p.search(text) for p in TABLET_INDICATOR_PATTERNS)
    # Generic brand words (samsung, mi, xiaomi via "mi note" etc.) shouldn't
    # tag a listing as "mobile" if it's actually a tablet (e.g. "Galaxy Tab",
    # "iPad", "Mi Pad"). Phone-specific keywords (iphone, oneplus, redmi,
    # poco, etc.) still count even if the word "tablet" appears elsewhere.
    GENERIC_BRAND_ONLY = {"samsung", "galaxy", "mi note", "xiaomi"}
    matched_mobile_kws = [kw for kw in MOBILE_KEYWORDS if kw in text]
    if looks_like_tablet_listing:
        matched_mobile_kws = [kw for kw in matched_mobile_kws if kw not in GENERIC_BRAND_ONLY]
    is_mobile = len(matched_mobile_kws) > 0

    is_laptop = any(kw in text for kw in LAPTOP_KEYWORDS)

    is_tablet = False
    if any(p.search(text) for p in TABLET_PATTERNS):
        # make sure it's not an explicitly excluded older model mentioned instead
        if not any(p.search(text) for p in TABLET_EXCLUDE_PATTERNS) or any(
            p.search(text) for p in TABLET_PATTERNS
        ):
            is_tablet = True

    is_console = any(kw in text for kw in CONSOLE_KEYWORDS)

    return {
        "mobile": is_mobile,
        "laptop": is_laptop,
        "tablet": is_tablet,
        "console": is_console,
    }


# --------------------------------------------------------------------------
# FETCHING (unauthenticated public JSON endpoint)
# --------------------------------------------------------------------------

def fetch_new_posts(subreddit, sort="new", limit=50, retries=3):
    """
    Fetch posts from a subreddit's public JSON feed - no OAuth required.
    e.g. https://www.reddit.com/r/HyderabadBuySell/new.json?limit=50
    """
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            children = data.get("data", {}).get("children", [])
            return [c["data"] for c in children]
        except urllib.error.HTTPError as e:
            print(f"[warn] HTTP {e.code} fetching r/{subreddit}/{sort} "
                  f"(attempt {attempt}/{retries})", file=sys.stderr)
            if e.code == 429:
                time.sleep(5 * attempt)
            else:
                break
        except Exception as e:
            print(f"[warn] error fetching r/{subreddit}/{sort}: {e} "
                  f"(attempt {attempt}/{retries})", file=sys.stderr)
            time.sleep(3 * attempt)
    return []


# --------------------------------------------------------------------------
# STATE (dedup)
# --------------------------------------------------------------------------

def load_seen_ids():
    if os.path.exists(SEEN_IDS_FILE):
        with open(SEEN_IDS_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(seen_ids, cap=5000):
    # keep the file from growing forever - trim oldest by just capping size
    ids_list = list(seen_ids)[-cap:]
    with open(SEEN_IDS_FILE, "w") as f:
        json.dump(ids_list, f)


# --------------------------------------------------------------------------
# TODAY-ONLY FILTER
# --------------------------------------------------------------------------

def is_from_today(created_utc):
    """True if the UTC-timestamp falls on today's UTC calendar date."""
    created = datetime.fromtimestamp(created_utc, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    return created.date() == now.date()


def passes_age_filter(created_utc):
    """Combines the calendar-day check with the optional max-age cap."""
    now = datetime.now(timezone.utc)
    created = datetime.fromtimestamp(created_utc, tz=timezone.utc)

    if ONLY_TODAY and created.date() != now.date():
        return False

    if MAX_AGE_HOURS is not None:
        age_hours = (now - created).total_seconds() / 3600
        if age_hours > MAX_AGE_HOURS:
            return False

    return True


# --------------------------------------------------------------------------
# REPORTING
# --------------------------------------------------------------------------

def build_post_entry(post):
    permalink = f"https://www.reddit.com{post.get('permalink', '')}"
    title = post.get("title", "(no title)")
    body = post.get("selftext", "") or ""
    created = datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
    price = extract_price(f"{title} {body}")
    return {
        "title": title,
        "url": permalink,
        "subreddit": post.get("subreddit", ""),
        "price": price,
        "age_hours": round(age_hours, 1),
        "created_str": created.strftime("%Y-%m-%d %H:%M UTC"),
        "body_snippet": body[:200],
    }


def format_price(price):
    return f"₹{price:,}" if price is not None else "price not mentioned"


def render_report(mobiles, laptops, laptops_no_price, tablets, consoles):
    lines = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"# Hyderabad Buy/Sell Report - {ts}\n")

    lines.append("## Mobiles")
    if not mobiles:
        lines.append("_No new mobile listings found in this run._\n")
    for p in mobiles:
        lines.append(
            f"- **{p['title']}** - {format_price(p['price'])} - "
            f"[link]({p['url']}) - r/{p['subreddit']} - {p['age_hours']}h ago"
        )
    lines.append("")

    lines.append(f"## Laptops (<= Rs.{LAPTOP_MAX_PRICE:,})")
    if not laptops and not laptops_no_price:
        lines.append("_No new laptop listings within budget found in this run._\n")
    for p in laptops:
        lines.append(
            f"- **{p['title']}** - {format_price(p['price'])} - "
            f"[link]({p['url']}) - r/{p['subreddit']} - {p['age_hours']}h ago"
        )
    if laptops_no_price:
        lines.append("\n**Price not mentioned - needs follow-up:**")
        for p in laptops_no_price:
            lines.append(
                f"- **{p['title']}** - [link]({p['url']}) - "
                f"r/{p['subreddit']} - {p['age_hours']}h ago"
            )
    lines.append("")

    lines.append("## Tablets (iPad 10th-gen+/Air/Pro/Mini, Mi Pad 6/7/8 only)")
    if not tablets:
        lines.append("_No new matching tablet listings found in this run._\n")
    for p in tablets:
        lines.append(
            f"- **{p['title']}** - {format_price(p['price'])} - "
            f"[link]({p['url']}) - r/{p['subreddit']} - {p['age_hours']}h ago"
        )
    lines.append("")

    lines.append("## Game Consoles")
    if not consoles:
        lines.append("_No new game console listings found in this run._\n")
    for p in consoles:
        lines.append(
            f"- **{p['title']}** - {format_price(p['price'])} - "
            f"[link]({p['url']}) - r/{p['subreddit']} - {p['age_hours']}h ago"
        )
    lines.append("")

    return "\n".join(lines)


def send_ntfy_alert(content):
    if not NTFY_TOPIC:
        print("[info] NTFY_TOPIC not set - skipping push notification", file=sys.stderr)
        return
    body = content[:3800]  # keep the push notification body reasonable
    req = urllib.request.Request(
        NTFY_URL,
        data=body.encode("utf-8"),
        headers={
            "Title": "Hyderabad Buy/Sell - New Listings",
            "Priority": "default",
            "Tags": "bell",
            "Content-Type": "text/plain; charset=utf-8",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[warn] failed to send ntfy alert: {e}", file=sys.stderr)


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main():
    seen_ids = load_seen_ids()
    new_seen_ids = set(seen_ids)

    mobiles, laptops, laptops_no_price, tablets, consoles = [], [], [], [], []

    for subreddit in SUBREDDITS:
        for sort in SORTS:
            posts = fetch_new_posts(subreddit, sort=sort, limit=POSTS_PER_SUB)
            time.sleep(2)  # be polite between requests

            for post in posts:
                post_id = post.get("name") or post.get("id")
                if not post_id or post_id in seen_ids:
                    continue

                created_utc = post.get("created_utc", 0)
                if not passes_age_filter(created_utc):
                    # Still mark as seen so we don't re-evaluate it forever,
                    # but never report it since it's not from today.
                    new_seen_ids.add(post_id)
                    continue

                new_seen_ids.add(post_id)

                title = post.get("title", "")
                body = post.get("selftext", "") or ""
                flags = classify_post(title, body)

                if not any(flags.values()):
                    continue  # not a match for any category, skip

                entry = build_post_entry(post)

                if flags["mobile"]:
                    mobiles.append(entry)
                if flags["laptop"]:
                    price = entry["price"]
                    if price is None:
                        laptops_no_price.append(entry)
                    elif price <= LAPTOP_MAX_PRICE:
                        laptops.append(entry)
                    # else: over budget, skip per spec
                if flags["tablet"]:
                    tablets.append(entry)
                if flags["console"]:
                    consoles.append(entry)

    report = render_report(mobiles, laptops, laptops_no_price, tablets, consoles)

    with open(REPORT_FILE, "w") as f:
        f.write(report)

    print(report)

    total_new = len(mobiles) + len(laptops) + len(laptops_no_price) + len(tablets) + len(consoles)
    if total_new > 0:
        send_ntfy_alert(report)

    save_seen_ids(new_seen_ids)


if __name__ == "__main__":
    main()

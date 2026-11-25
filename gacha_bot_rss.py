#!/usr/bin/env python3
"""Gacha (capsule toy) news bot - RSS edition. No AI, no paid API: everything here is free.

Flow: read RSS feeds (feeds.txt) -> keep capsule-toy articles -> queue them
      -> post to Bluesky as: English one-liner + original Japanese headline + link card.

Usage:
  python gacha_bot_rss.py check     # test every feed in feeds.txt (reachable? how many matching articles?)
  python gacha_bot_rss.py collect   # collect only
  python gacha_bot_rss.py post      # post today's share only
  python gacha_bot_rss.py daily     # scheduled run: collect once per ISO week, then post today's share

Environment variables:
  BSKY_HANDLE           e.g. yourname.bsky.social  (required unless DRY_RUN)
  BSKY_APP_PASSWORD     Bluesky *app password*      (required unless DRY_RUN)
  REQUIRE_APPROVAL      "true" (default): only items with "approved": true in state/queue.json are posted
  DRY_RUN               "true": print what would be posted, post nothing
  MAX_ITEMS_PER_WEEK    default 10
  MAX_PER_SOURCE        default 5   (max items from a single feed per week)
  MAX_AGE_DAYS          default 14  (ignore older articles)
  FEEDS_FILE            default feeds.txt
  STATE_DIR             default state
"""
import datetime as dt
import html
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
QUEUE_PATH = STATE_DIR / "queue.json"
POSTED_PATH = STATE_DIR / "posted.json"
FEEDS_PATH = Path(os.environ.get("FEEDS_FILE", "feeds.txt"))

MAX_ITEMS_PER_WEEK = int(os.environ.get("MAX_ITEMS_PER_WEEK", "10"))
MIN_ITEMS_PER_WEEK = 5
MAX_PER_SOURCE = int(os.environ.get("MAX_PER_SOURCE", "5"))
MAX_AGE_DAYS = int(os.environ.get("MAX_AGE_DAYS", "14"))
STALE_QUEUE_DAYS = 30
REQUIRE_APPROVAL = os.environ.get("REQUIRE_APPROVAL", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MAX_POST_LEN = 280  # Bluesky's limit is 300 graphemes; keep a safety margin

UA = "Mozilla/5.0 (compatible; GachaNewsBot/1.0; +feed-reader)"

# Candidate feeds. These are guesses: run `check` once and edit feeds.txt based on the result.
# A page URL (not just a feed URL) also works: the bot looks for the feed link inside the page.
DEFAULT_FEEDS = [
    # capsule toy / gashapon news sites
    ("https://www.toy-people.com/en/", "Toy People (English)"),
    ("https://hobby.dengeki.com/tag/gatya/feed/", "Dengeki Hobby (gacha tag)"),
    ("https://www.itmedia.co.jp/keywords/gashapon.html", "ITmedia (gashapon keyword)"),
    # maker press releases on PR TIMES
    ("https://prtimes.jp/companyrdf.php?company_id=122810", "PR TIMES (Bandai)"),
    ("https://prtimes.jp/companyrdf.php?company_id=166988", "PR TIMES (Kenelephant)"),
    # sites that republish PR TIMES releases (general news; the keyword filter keeps capsule toys only)
    ("https://www.inside-games.jp/rss/index.rdf", "Inside"),
    ("https://www.gamespark.jp/rss/index.rdf", "Game*Spark"),
    ("https://animeanime.jp/rss/index.rdf", "Anime Anime"),
    ("https://resemom.jp/rss/index.rdf", "Resemom"),
]

# Only articles that clearly talk about capsule toys. Plain "ガチャ" is left out on purpose
# because it matches mobile-game gacha news.
KEYWORD_RE = re.compile(r"ガシャポン|ガチャガチャ|カプセルトイ|カプセル\s*トイ|gashapon|gachapon|capsule\s*toy", re.I)
EXCLUDE_RE = re.compile(r"ピックアップ|召喚|ソシャゲ|スマホゲーム|スマートフォンゲーム|アプリ|無料\s*\d+\s*連|イベントガチャ")


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- state

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_posted():
    data = load_json(POSTED_PATH, {})
    data.setdefault("posted", [])
    data.setdefault("last_collect_week", None)
    return data


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def norm(s):
    return re.sub(r"[\s　・!！~〜\-ー「」『』]", "", s or "").lower()


def item_key(item):
    return item.get("key") or item.get("official_url")


def item_title(item):
    return item.get("title") or item.get("name_ja") or item_key(item)


# ---------------------------------------------------------------- feeds

def load_feed_list():
    if not FEEDS_PATH.exists():
        log(f"Feed list: '{FEEDS_PATH}' not found in the repository root -> using the built-in list ({len(DEFAULT_FEEDS)} feeds).")
        return DEFAULT_FEEDS
    feeds = []
    for line in FEEDS_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url, _, label = line.partition("|")
        feeds.append((url.strip(), label.strip() or url.strip()))
    if not feeds:
        log(f"Feed list: '{FEEDS_PATH}' has no feed lines -> using the built-in list ({len(DEFAULT_FEEDS)} feeds).")
        return DEFAULT_FEEDS
    log(f"Feed list: using '{FEEDS_PATH}' ({len(feeds)} feeds).")
    return feeds


def _get(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    except requests.RequestException as e:
        log(f"  fetch failed ({url}): {e}")
        return None
    return r if r.status_code == 200 else None


def fetch_feed(url):
    """Return (parsed_feed, used_url) or None. If `url` is an HTML page, look for its feed link."""
    r = _get(url)
    if r is None:
        return None
    parsed = feedparser.parse(r.content)
    if parsed.entries:
        return parsed, url
    soup = BeautifulSoup(r.text, "html.parser")
    for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
        kind = (link.get("type") or "").lower()
        href = link.get("href")
        if not href or not ("rss" in kind or "atom" in kind or "xml" in kind):
            continue
        feed_url = urljoin(url, href)
        r2 = _get(feed_url)
        if r2 is None:
            continue
        parsed = feedparser.parse(r2.content)
        if parsed.entries:
            return parsed, feed_url
    return None


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(BeautifulSoup(s or "", "html.parser").get_text(" "))).strip()


def entries_from(parsed, label):
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = strip_tags(e.get("title"))
        if not link or not title:
            continue
        t = e.get("published_parsed") or e.get("updated_parsed")
        published = dt.datetime(*t[:6], tzinfo=dt.timezone.utc) if t else None
        yield {"title": title, "link": link, "summary": strip_tags(e.get("summary")),
               "published": published, "source": label}


def is_relevant(c):
    text = f"{c['title']} {c['summary']}"
    return bool(KEYWORD_RE.search(text)) and not EXCLUDE_RE.search(c["title"])


# ---------------------------------------------------------------- compose

def compose_text(title):
    gashapon = bool(re.search(r"ガシャポン|gashapon", title, re.I))
    header = f"🇯🇵 New {'Gashapon' if gashapon else 'capsule toy'} news from Japan"
    tags = "#Gashapon #CapsuleToy #ガシャポン" if gashapon else "#CapsuleToy #Gachagacha #カプセルトイ"
    budget = MAX_POST_LEN - len(header) - len(tags) - 3
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) > budget:
        title = title[:budget - 1].rstrip() + "…"
    return f"{header}\n{title}\n\n{tags}"


def fetch_card(url, fallback_title):
    """Link-card data (title, description, OGP image) from the article page; None if unreachable."""
    r = _get(url)
    if r is None:
        return None
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    def og(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else ""

    title = og("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    image = og("og:image")
    return {
        "title": (title or fallback_title)[:120],
        "description": og("og:description")[:280],
        "image_url": urljoin(url, image) if image else "",
    }


# ---------------------------------------------------------------- collect

def collect():
    now = now_utc()
    feeds = load_feed_list()
    log(f"Reading {len(feeds)} feed(s)...")
    candidates = []
    for url, label in feeds:
        res = fetch_feed(url)
        if not res:
            log(f"  WARN: no usable feed at {url} ({label}); skipped")
            continue
        parsed, used = res
        count = 0
        for c in entries_from(parsed, label):
            if not is_relevant(c):
                continue
            if c["published"] and (now - c["published"]).days > MAX_AGE_DAYS:
                continue
            candidates.append(c)
            count += 1
        log(f"  {label}: {count} matching article(s)")
    candidates.sort(key=lambda c: c["published"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)

    queue = load_json(QUEUE_PATH, [])
    queue = [q for q in queue if not _is_stale(q, now)]
    posted = load_posted()
    seen_keys = {item_key(q) for q in queue} | {p["key"] for p in posted["posted"] if "key" in p}
    seen_titles = {norm(item_title(q)) for q in queue} | {norm(p.get("title", "")) for p in posted["posted"]}
    per_source, added = {}, 0
    for c in candidates:
        if len(queue) >= MAX_ITEMS_PER_WEEK:
            break
        if c["link"] in seen_keys or norm(c["title"]) in seen_titles:
            continue
        if per_source.get(c["source"], 0) >= MAX_PER_SOURCE:
            continue
        log(f"- {c['title']}")
        card = fetch_card(c["link"], c["title"])
        if not card:
            log("  page not reachable, skipped")
            continue
        queue.append({
            "key": c["link"],
            "title": c["title"],
            "source": c["source"],
            "published": c["published"].isoformat(timespec="seconds") if c["published"] else None,
            "official_url": c["link"],
            "card": card,
            "text": compose_text(c["title"]),  # editable before approval
            "approved": not REQUIRE_APPROVAL,
            "collected_at": now.isoformat(timespec="seconds"),
        })
        seen_keys.add(c["link"])
        seen_titles.add(norm(c["title"]))
        per_source[c["source"]] = per_source.get(c["source"], 0) + 1
        added += 1
        log("  queued")
    save_json(QUEUE_PATH, queue)
    log(f"Collected {added} new item(s); queue now {len(queue)}.")
    if added < MIN_ITEMS_PER_WEEK:
        log(f"WARNING: fewer than {MIN_ITEMS_PER_WEEK} new items this week (maybe add more feeds to feeds.txt).")
    return added


def _is_stale(item, now):
    ts = item.get("collected_at")
    if not ts:
        return False
    try:
        return (now - dt.datetime.fromisoformat(ts)).days > STALE_QUEUE_DAYS
    except ValueError:
        return False


def check():
    for url, label in load_feed_list():
        res = fetch_feed(url)
        if not res:
            log(f"[NG] {label}: {url}  (not reachable, or no feed found)")
            continue
        parsed, used = res
        entries = list(entries_from(parsed, label))
        relevant = [e for e in entries if is_relevant(e)]
        log(f"[OK] {label}: {used}  entries={len(entries)} matching={len(relevant)}")
        for e in relevant[:3]:
            log(f"       - {e['title']}")


# ---------------------------------------------------------------- post

TAG_RE = re.compile(r"(#[0-9A-Za-z_぀-ヿ㐀-鿿]+)")


def rich_text(text):
    from atproto import client_utils

    tb = client_utils.TextBuilder()
    for part in TAG_RE.split(text):
        if not part:
            continue
        if TAG_RE.fullmatch(part):
            tb.tag(part, part[1:])
        else:
            tb.text(part)
    return tb


def fetch_thumb(image_url):
    """Download the OGP image and shrink it under Bluesky's blob size limit."""
    if not image_url:
        return None
    from PIL import Image

    try:
        r = requests.get(image_url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:  # noqa: BLE001
        log(f"  thumbnail unavailable: {e}")
        return None
    img.thumbnail((1200, 1200))
    for quality in (85, 75, 65, 55):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        if buf.tell() <= 900_000:
            return buf.getvalue()
    return None


def post_item(client, item):
    from atproto import models

    thumb = fetch_thumb(item["card"].get("image_url"))
    blob = client.upload_blob(thumb).blob if thumb else None
    external = models.AppBskyEmbedExternal.External(
        uri=item["official_url"],
        title=item["card"]["title"],
        description=item["card"]["description"],
        thumb=blob,
    )
    embed = models.AppBskyEmbedExternal.Main(external=external)
    resp = client.send_post(rich_text(item["text"]), embed=embed, langs=["en", "ja"])
    return resp.uri


def post_today():
    now = now_utc()
    today = now.date().isoformat()
    queue = load_json(QUEUE_PATH, [])
    state = load_posted()
    posted_today = sum(1 for p in state["posted"] if p.get("posted_at", "").startswith(today))
    postable = [q for q in queue if q.get("approved")]
    days_left = 7 - now.weekday()  # Mon=7 ... Sun=1: spreads the queue across the rest of the week
    total_today = math.ceil((len(postable) + posted_today) / days_left)
    n = max(0, min(total_today - posted_today, len(postable)))
    log(f"Queue {len(queue)}, approved {len(postable)}, posted today {posted_today}; posting {n} now.")
    if n == 0:
        if queue and not postable and REQUIRE_APPROVAL:
            log("Nothing approved yet. Set \"approved\": true on items in state/queue.json.")
        return True

    client = None
    if not DRY_RUN:
        from atproto import Client

        client = Client()
        client.login(os.environ["BSKY_HANDLE"], os.environ["BSKY_APP_PASSWORD"])

    ok = True
    for i, item in enumerate(postable[:n]):
        if DRY_RUN:
            log(f"--- DRY RUN ---\n{item['text']}\n[card] {item['official_url']}\n")
            continue
        try:
            uri = post_item(client, item)
        except Exception as e:  # noqa: BLE001
            log(f"Post failed for {item_title(item)}: {e}")
            item["fail_count"] = item.get("fail_count", 0) + 1
            ok = False
            save_json(QUEUE_PATH, queue)
            continue
        queue = [q for q in queue if item_key(q) != item_key(item)]
        state["posted"].append({"key": item_key(item), "title": item_title(item),
                                "uri": uri, "posted_at": now_utc().isoformat(timespec="seconds")})
        save_json(QUEUE_PATH, queue)  # persist after every post to avoid double posting
        save_json(POSTED_PATH, state)
        log(f"Posted: {item_title(item)} -> {uri}")
        if i < n - 1:
            time.sleep(30)
    return ok


# ---------------------------------------------------------------- entry

def daily():
    ok = True
    state = load_posted()
    iso = now_utc().isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    if state.get("last_collect_week") != week:
        try:
            collect()
            state = load_posted()
            state["last_collect_week"] = week
            save_json(POSTED_PATH, state)
        except Exception as e:  # noqa: BLE001
            log(f"Collect failed: {e}")
            ok = False
    return post_today() and ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if cmd == "check":
        check()
    elif cmd == "collect":
        collect()
    elif cmd == "post":
        sys.exit(0 if post_today() else 1)
    elif cmd == "daily":
        sys.exit(0 if daily() else 1)
    else:
        print(__doc__)
        sys.exit(2)

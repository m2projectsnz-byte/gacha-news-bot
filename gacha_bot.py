#!/usr/bin/env python3
"""Gacha (capsule toy) news bot.

Flow: collect (Claude API + web search) -> verify official page -> compose EN+JA post
      -> post to Bluesky with a link card (image comes from the official page's OGP image).

Usage:
  python gacha_bot.py daily     # scheduled run: collect once per ISO week, then post today's share
  python gacha_bot.py collect   # collect only
  python gacha_bot.py post      # post today's share only

Environment variables:
  ANTHROPIC_API_KEY     required for collect
  BSKY_HANDLE           e.g. yourname.bsky.social  (required unless DRY_RUN)
  BSKY_APP_PASSWORD     Bluesky *app password*      (required unless DRY_RUN)
  ANTHROPIC_MODEL       default: claude-sonnet-5-5 (must support the web search tool)
  REQUIRE_APPROVAL      "true" (default): only items with "approved": true in state/queue.json are posted
  DRY_RUN               "true": print what would be posted, post nothing
  MAX_ITEMS_PER_WEEK    default 10
  STRICT_NAME_CHECK     "true" (default): drop items whose name is not found on the official page
  STATE_DIR             default: state
"""
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
QUEUE_PATH = STATE_DIR / "queue.json"
POSTED_PATH = STATE_DIR / "posted.json"

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
MAX_ITEMS_PER_WEEK = int(os.environ.get("MAX_ITEMS_PER_WEEK", "10"))
MIN_ITEMS_PER_WEEK = 5
MAX_PER_MAKER = 4
REQUIRE_APPROVAL = os.environ.get("REQUIRE_APPROVAL", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
STRICT_NAME_CHECK = os.environ.get("STRICT_NAME_CHECK", "true").lower() == "true"
MAX_POST_LEN = 280  # Bluesky's limit is 300 graphemes; keep a safety margin
LOOKAHEAD_MONTHS = 6

# The link in a post must be on one of these domains (official makers / their press releases).
OFFICIAL_DOMAINS = tuple(
    d.strip()
    for d in os.environ.get(
        "OFFICIAL_DOMAINS",
        "gashapon.jp,bandai.co.jp,takaratomy-arts.co.jp,kenelephant.co.jp,"
        "kenelestore.jp,kaiyodo.co.jp,prtimes.jp",
    ).split(",")
    if d.strip()
)
# Domains Claude may search (official sites + one aggregator for discovery only).
SEARCH_DOMAINS = list(OFFICIAL_DOMAINS) + ["toy-people.com"]

UA = "Mozilla/5.0 (compatible; GachaNewsBot/1.0; +link-preview)"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

COLLECT_PROMPT = """You are researching new Japanese capsule toys (gashapon / gachagacha) for an English-language news feed.

Today is {today}. Use web search to find products from these makers whose release month is between {start} and {end}:
- Bandai Gashapon (gashapon.jp)
- Takara Tomy Arts / T-ARTS
- Kenelephant
- Kaiyodo
Prefer the maker's own announcement pages or press releases. Aggregator sites may help you find items, but the official_url you return must be on one of these domains: {domains}.

Return up to {n} items, mixing makers (no more than {per_maker} per maker). Skip items already released before the start date, rumors, and anything you cannot tie to an official page.

Output ONLY a JSON array (no markdown fences, no commentary). Each element:
{{
  "name_ja": "official Japanese product name",
  "name_en": "official English name if one exists, otherwise a short romanization/translation of the Japanese name",
  "maker_ja": "one of: バンダイ, タカラトミーアーツ, ケンエレファント, 海洋堂",
  "maker_en": "one of: Bandai, Takara Tomy Arts, Kenelephant, Kaiyodo",
  "price_yen": integer price per spin including tax, or null,
  "lineup_count": number of designs in the series, or null,
  "release_month": "YYYY-MM",
  "blurb_en": "one plain factual sentence about what the product is, max 90 characters, no hype; do NOT repeat the price, release date or number of designs (those are added separately)",
  "blurb_ja": "same content in one Japanese sentence, max 40 characters; do NOT repeat the price, release date or number of designs",
  "emoji": "one fitting emoji",
  "official_url": "https URL of the official page for this exact product, or the official monthly lineup page that lists it"
}}
Use only facts that appear in the sources. Use null when unknown. Never invent URLs."""


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
    return re.sub(r"[\s\u3000・!！~〜\-ー]", "", s or "").lower()


def dedupe_key(item):
    return f"{norm(item['name_ja'])}|{item['release_month']}"


def month_index(ym):
    y, m = ym.split("-")
    return int(y) * 12 + int(m) - 1


# ---------------------------------------------------------------- collect

def is_official(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in OFFICIAL_DOMAINS)


def parse_json_array(text):
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def ask_claude_for_candidates(now):
    import anthropic

    start = now.strftime("%Y-%m")
    end_idx = month_index(start) + LOOKAHEAD_MONTHS
    end = f"{end_idx // 12}-{end_idx % 12 + 1:02d}"
    prompt = COLLECT_PROMPT.format(
        today=now.date().isoformat(), start=start, end=end,
        domains=", ".join(OFFICIAL_DOMAINS), n=MAX_ITEMS_PER_WEEK + 4, per_maker=MAX_PER_MAKER,
    )
    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20250305", "name": "web_search",
              "max_uses": 12, "allowed_domains": SEARCH_DOMAINS}]
    messages = [{"role": "user", "content": prompt}]
    resp = None
    for _ in range(6):  # server-side tool turns can pause; continue until done
        resp = client.messages.create(model=MODEL, max_tokens=8000, tools=tools, messages=messages)
        if resp.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": resp.content})
    text = "".join(b.text for b in resp.content if b.type == "text")
    return parse_json_array(text)


def validate_candidate(c, now):
    """Return a cleaned item or None."""
    try:
        item = {
            "name_ja": str(c["name_ja"]).strip(),
            "name_en": str(c["name_en"]).strip(),
            "maker_ja": str(c["maker_ja"]).strip(),
            "maker_en": str(c["maker_en"]).strip(),
            "price_yen": int(c["price_yen"]) if c.get("price_yen") else None,
            "lineup_count": int(c["lineup_count"]) if c.get("lineup_count") else None,
            "release_month": str(c["release_month"]).strip(),
            "blurb_en": (c.get("blurb_en") or "").strip(),
            "blurb_ja": (c.get("blurb_ja") or "").strip(),
            "emoji": (c.get("emoji") or "🎁").strip()[:2] or "🎁",
            "official_url": str(c["official_url"]).strip(),
        }
    except (KeyError, TypeError, ValueError):
        return None
    if not item["name_ja"] or not item["name_en"]:
        return None
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", item["release_month"]):
        return None
    cur = month_index(now.strftime("%Y-%m"))
    if not (cur <= month_index(item["release_month"]) <= cur + LOOKAHEAD_MONTHS):
        return None  # already released or too far ahead
    if not item["official_url"].startswith("https://") or not is_official(item["official_url"]):
        return None
    return item


def fetch_card(url, name_ja):
    """Fetch the official page; return link-card data or None if it fails verification."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    except requests.RequestException as e:
        log(f"  fetch failed: {e}")
        return None
    if r.status_code != 200:
        log(f"  HTTP {r.status_code}")
        return None
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    def og(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") or "").strip() if tag else ""

    title = og("og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
    if STRICT_NAME_CHECK and norm(name_ja) not in norm(soup.get_text(" ")) and norm(name_ja) not in norm(title):
        log("  product name not found on page")
        return None
    image = og("og:image")
    return {
        "title": (title or name_ja)[:120],
        "description": og("og:description")[:280],
        "image_url": urljoin(url, image) if image else "",
    }


def month_en(ym):
    y, m = ym.split("-")
    return f"{MONTHS[int(m) - 1]} {y}"


def compose_text(item, with_blurbs=True):
    """Pattern A: English block, blank line, Japanese block, hashtags."""
    bandai = "bandai" in item["maker_en"].lower()
    en_parts = []
    if item.get("lineup_count"):
        en_parts.append(f"{item['lineup_count']} designs")
    if item.get("price_yen"):
        en_parts.append(f"¥{item['price_yen']:,}/spin")
    en_parts.append(f"{month_en(item['release_month'])} in Japan")
    en_details = ", ".join(en_parts) + "."
    blurb_en = item["blurb_en"].rstrip(".") + "." if with_blurbs and item["blurb_en"] else ""

    y, m = item["release_month"].split("-")
    ja_parts = []
    if item.get("lineup_count"):
        ja_parts.append(f"全{item['lineup_count']}種")
    if item.get("price_yen"):
        ja_parts.append(f"1回{item['price_yen']}円")
    ja_parts.append(f"{y}年{int(m)}月発売予定")
    ja_details = "、".join(ja_parts) + "。"
    blurb_ja = item["blurb_ja"].rstrip("。") + "。" if with_blurbs and item["blurb_ja"] else ""

    kind_en = "Gashapon" if bandai else "capsule toy"
    kind_ja = "ガシャポン" if bandai else "カプセルトイ"
    tags = "#Gashapon #CapsuleToy #ガシャポン" if bandai else "#CapsuleToy #Gachagacha #カプセルトイ"
    lines = [
        f"New {kind_en} from {item['maker_en']}: \"{item['name_en']}\" {item['emoji']}",
        f"{blurb_en} {en_details}".strip(),
        "",
        f"{item['maker_ja']}新作{kind_ja}「{item['name_ja']}」",
        f"{blurb_ja}{ja_details}",
        tags,
    ]
    return "\n".join(lines)


def fit_text(item):
    text = compose_text(item)
    if len(text) > MAX_POST_LEN:
        text = compose_text(item, with_blurbs=False)
    return text if len(text) <= MAX_POST_LEN else None


def collect():
    now = now_utc()
    log("Collecting candidates via Claude web search...")
    candidates = ask_claude_for_candidates(now)
    log(f"  {len(candidates)} raw candidates")

    queue = load_json(QUEUE_PATH, [])
    cur = month_index(now.strftime("%Y-%m"))
    queue = [q for q in queue if month_index(q["release_month"]) >= cur]  # drop stale
    posted = load_posted()
    seen = {dedupe_key(q) for q in queue} | {p["key"] for p in posted["posted"] if "key" in p}
    per_maker = {}
    added = 0
    for c in candidates:
        if len(queue) >= MAX_ITEMS_PER_WEEK:
            break
        item = validate_candidate(c, now)
        if not item:
            continue
        log(f"- {item['name_ja']} ({item['release_month']})")
        if dedupe_key(item) in seen:
            log("  duplicate, skipped")
            continue
        if per_maker.get(item["maker_en"], 0) >= MAX_PER_MAKER:
            continue
        card = fetch_card(item["official_url"], item["name_ja"])
        if not card:
            continue
        text = fit_text(item)
        if not text:
            log("  post too long, skipped")
            continue
        item.update({
            "card": card,
            "text": text,  # editable before approval
            "approved": not REQUIRE_APPROVAL,
            "collected_at": now.isoformat(timespec="seconds"),
        })
        queue.append(item)
        seen.add(dedupe_key(item))
        per_maker[item["maker_en"]] = per_maker.get(item["maker_en"], 0) + 1
        added += 1
        log("  queued")
    save_json(QUEUE_PATH, queue)
    log(f"Collected {added} new item(s); queue now {len(queue)}.")
    if added < MIN_ITEMS_PER_WEEK:
        log(f"WARNING: fewer than {MIN_ITEMS_PER_WEEK} new items this week.")
    return added


# ---------------------------------------------------------------- post

TAG_RE = re.compile(r"(#[0-9A-Za-z_\u3040-\u30ff\u3400-\u9fff]+)")


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
            log(f"Post failed for {item['name_ja']}: {e}")
            item["fail_count"] = item.get("fail_count", 0) + 1
            ok = False
            save_json(QUEUE_PATH, queue)
            continue
        queue = [q for q in queue if dedupe_key(q) != dedupe_key(item)]
        state["posted"].append({"key": dedupe_key(item), "name_ja": item["name_ja"],
                                "uri": uri, "posted_at": now_utc().isoformat(timespec="seconds")})
        save_json(QUEUE_PATH, queue)  # persist after every post to avoid double posting
        save_json(POSTED_PATH, state)
        log(f"Posted: {item['name_ja']} -> {uri}")
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
    if cmd == "collect":
        collect()
    elif cmd == "post":
        sys.exit(0 if post_today() else 1)
    elif cmd == "daily":
        sys.exit(0 if daily() else 1)
    else:
        print(__doc__)
        sys.exit(2)

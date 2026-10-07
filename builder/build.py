#!/usr/bin/env python3
"""
Khobor Duniya — static Bangla world-news aggregator builder.

Pipeline:
  1. Fetch world-news RSS feeds (free, no API key).
  2. Dedupe by canonical URL, keep newest ~150 items.
  3. Machine-translate title + short summary en -> bn via MyMemory
     (free tier). Translations are cached in data/translations.json
     keyed by sha1 of the English text, so an item is NEVER
     re-translated and quota is only spent on genuinely new text.
  4. Classify each item into a Bangla category with keyword rules.
  5. Write data/news.json and render the static site into site/.

Legal model: headlines + short translated summaries + prominent links
to the original articles. Full stories stay on the publishers' sites.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
ASSETS_DIR = ROOT / "assets"

NEWS_JSON = DATA_DIR / "news.json"
CACHE_JSON = DATA_DIR / "translations.json"

BASE_URL = "https://mdnaimislambd.github.io/khobor-duniya/"
DHAKA = ZoneInfo("Asia/Dhaka")

HTTP_HEADERS = {
    "User-Agent": "KhoborDuniya/1.0 (+https://mdnaimislambd.github.io/khobor-duniya/)",
}

# ---------------------------------------------------------------- feeds
# NOTE (2026-10-08): AP's feed (apnews.com/hub/world-news?format=rss) returns
# HTTP 403 (Cloudflare bot check) and was dropped. The rest verified live.
FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("DW", "https://rss.dw.com/rdf/rss-en-all"),
    ("France 24", "https://www.france24.com/en/rss"),
    ("The Guardian", "https://www.theguardian.com/world/rss"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
]

MAX_ITEMS = 150          # newest items kept in news.json
SUMMARY_CHARS = 350      # description truncated before translation (quota)
ARTICLE_PAGE_DAYS = 7    # static article pages only for items this fresh

# ---------------------------------------------------------------- translate
MYMEMORY_URL = "https://api.mymemory.translated.net/get"
# MyMemory free quota: ~5,000 chars/day anonymous, ~50,000 chars/day when a
# valid contact email is passed as `de`. We stay well under both per run.
BUDGET_ANON = 4_500
BUDGET_EMAIL = 45_000


def sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def mymemory_translate(text: str, email: str | None) -> str | None:
    """Translate one short English text to Bengali. None = failed/quota out."""
    params = {"q": text, "langpair": "en|bn"}
    if email:
        params["de"] = email
    try:
        r = requests.get(MYMEMORY_URL, params=params, headers=HTTP_HEADERS, timeout=25)
        data = r.json()
    except Exception as e:
        print(f"    [translate] request failed: {e}", file=sys.stderr)
        return None
    if str(data.get("responseStatus")) != "200":
        print(f"    [translate] bad status: {data.get('responseStatus')}", file=sys.stderr)
        return None
    out = (data.get("responseData") or {}).get("translatedText") or ""
    out = out.strip()
    # MyMemory reports quota/usage problems as HTTP 200 with a warning text.
    if not out or "MYMEMORY WARNING" in out or "QUERY LENGTH LIMIT" in out:
        print(f"    [translate] quota/error payload: {out[:80]}", file=sys.stderr)
        return None
    return out


def translate_cached(text, cache, budget, email):
    # type: (str, dict, dict, str | None) -> str | None
    """Translate with persistent cache; decrements the run's char budget."""
    if not text:
        return None
    key = sha1(text)
    if key in cache:
        return cache[key]
    if budget["left"] <= 0:
        return None  # budget spent; item stays English until a later run
    # 500 bytes/request limit: our texts are pre-truncated, but stay safe.
    chunks, cur = [], ""
    for word in text.split():
        if len((cur + " " + word).encode("utf-8")) > 450:
            chunks.append(cur)
            cur = word
        else:
            cur = (cur + " " + word).strip()
    if cur:
        chunks.append(cur)
    parts = []
    for ch in chunks:
        size = len(ch.encode("utf-8"))
        if size > budget["left"]:
            return None
        t = mymemory_translate(ch, email)
        if t is None:
            return None  # stop this run's translations on any hard failure
        parts.append(t)
        budget["left"] -= size
        cache[key] = " ".join(parts)  # partial progress is still cached
        time.sleep(0.4)  # be polite to the free API
    result = " ".join(parts)
    cache[key] = result
    return result


# ---------------------------------------------------------------- rss parsing
def canonical_url(url: str) -> str:
    """Normalize for dedupe: drop query/fragment, trailing slash."""
    p = urlparse(url.strip())
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", "", ""))


def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut + "…"


def local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def parse_feed_xml(source, url):
    # type: (str, str) -> list
    """Fetch one RSS/RDF feed -> list of raw item dicts. Never raises."""
    items = []
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"[feed] {source}: fetch failed ({e})", file=sys.stderr)
        return items
    for el in root.iter():
        if local_name(el.tag) != "item":
            continue
        title = link = desc = pub = ""
        for child in el:
            name = local_name(child.tag).lower()
            val = (child.text or "").strip()
            if name == "title" and not title:
                title = clean_text(val)
            elif name == "link" and not link:
                link = val or child.attrib.get("href", "")
            elif name in ("description", "encoded", "summary") and not desc:
                desc = clean_text(val)
            elif name in ("pubdate", "date", "published", "updated") and not pub:
                pub = val
        if not title or not link:
            continue
        # skip "read more"-style junk descriptions
        if desc and len(desc) < 40:
            desc = ""
        items.append({
            "source": source,
            "title_en": truncate(title, 220),
            "summary_en": truncate(desc, SUMMARY_CHARS),
            "url": canonical_url(link),
            "published": parse_date(pub),
        })
    print(f"[feed] {source}: {len(items)} items")
    return items


def parse_date(s: str) -> str:
    """Any date string -> ISO-8601 UTC. Fallback: now."""
    if s:
        try:
            dt = parsedate_to_datetime(s)
        except (ValueError, TypeError):
            dt = None
        if dt is None:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                dt = None
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- classify
CATEGORY_RULES = [
    # Strong political signals win outright (e.g. "election ... climate"
    # is politics first, not science).
    ("রাজনীতি", ["election", " vote", "parliament", "president",
                 "prime minister", "referendum", "coup", "minister"]),
    ("খেলা", ["sport", "football", "cricket", "match", "fifa", "olympic",
              "tennis", "goal", "league", "championship", "tournament",
              "world cup", "player", "coach", "victory", "defeat"]),
    ("বিজ্ঞান", ["scien", "study", "research", "nasa", "space", "climate",
                 "discover", "vaccine", "disease", "health", "ebola",
                 "planet", "fossil"]),
    ("প্রযুক্তি", ["technolog", " tech ", "ai ", "artificial intelligence",
                   "software", "smartphone", "google", "apple", "microsoft",
                   "robot", "cyber", "internet", "startup", " chip",
                   "app ", "data center"]),
    ("অর্থনীতি", ["econom", "market", "stock", "inflation", "trade", "bank",
                  "financ", "gdp", "dollar", "oil price", "business",
                  "company", "tariff", "interest rate", "recession"]),
    ("রাজনীতি", ["government", "protest",
                 "war ", "ceasefire", "diplom", "sanction", "military",
                 "summit", "treaty", "border"]),
]
DEFAULT_CATEGORY = "বিশ্ব"
CATEGORIES = ["সর্বশেষ", "বিশ্ব", "রাজনীতি", "অর্থনীতি", "প্রযুক্তি", "খেলা", "বিজ্ঞান"]


def classify(title_en, summary_en):
    # type: (str, str) -> str
    text = (" " + title_en + " " + summary_en + " ").lower()
    for cat, keywords in CATEGORY_RULES:
        if any(k in text for k in keywords):
            return cat
    return DEFAULT_CATEGORY


# ---------------------------------------------------------------- bangla formatting
BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
BN_MONTHS = ["", "জানুয়ারি", "ফেব্রুয়ারি", "মার্চ", "এপ্রিল", "মে", "জুন",
             "জুলাই", "আগস্ট", "সেপ্টেম্বর", "অক্টোবর", "নভেম্বর", "ডিসেম্বর"]


def bn_num(n):
    # type: (object) -> str
    return str(n).translate(BN_DIGITS)


def bn_datetime(iso):
    # type: (str) -> str
    """ISO UTC -> '৮ অক্টোবর ২০২৬, রাত ১:৩৪' (Asia/Dhaka)."""
    dt = datetime.fromisoformat(iso).astimezone(DHAKA)
    h = dt.hour
    if 4 <= h < 10:
        period = "সকাল"
    elif 10 <= h < 16:
        period = "দুপুর"
    elif 16 <= h < 18:
        period = "বিকাল"
    elif 18 <= h < 20:
        period = "সন্ধ্যা"
    else:
        period = "রাত"
    h12 = h % 12 or 12
    return (f"{bn_num(dt.day)} {BN_MONTHS[dt.month]} {bn_num(dt.year)}, "
            f"{period} {bn_num(h12)}:{bn_num(f'{dt.minute:02d}')}")


def make_slug(title_en, url):
    # type: (str, str) -> str
    base = re.sub(r"[^a-z0-9]+", "-", title_en.lower()).strip("-")[:55] or "news"
    return f"{base}-{sha1(url)[:8]}"


# ---------------------------------------------------------------- html templates
PAGE_HEAD = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} | খবর দুনিয়া</title>
<meta name="description" content="{meta_desc}">
<link rel="stylesheet" href="{root}assets/style.css">
<script src="{root}assets/config.js"></script>
{extra_head}
</head>
<body>
<header class="site-header">
  <div class="wrap">
    <h1><a href="{root}">খবর দুনিয়া</a></h1>
    <p class="tagline">বিশ্বের তাজা খবর, এখন বাংলায়</p>
  </div>
</header>
"""

PAGE_FOOT = """
<footer class="site-footer">
  <div class="wrap">
    <div class="disclaimer">
      <strong>দাবিত্যাগ:</strong> খবর দুনিয়া একটি স্বয়ংক্রিয় সংবাদ সংগ্রাহক।
      এখানে প্রকাশিত শিরোনাম ও সংক্ষিপ্তসার মূল ইংরেজি সংবাদের স্বয়ংক্রিয়
      বাংলা অনুবাদ — এতে ভুল থাকতে পারে। পূর্ণাঙ্গ ও নির্ভুল তথ্যের জন্য মূল
      প্রকাশকের ওয়েবসাইটে সংবাদটি পড়ুন। সব সংবাদের স্বত্ব সংশ্লিষ্ট প্রকাশকের।
    </div>
    <p><strong>উৎস:</strong> BBC • Al Jazeera • DW • France 24 • The Guardian • NPR</p>
    <p>© {year} খবর দুনিয়া — স্বয়ংক্রিয়ভাবে হালনাগাদ হয়</p>
  </div>
</footer>
</body>
</html>
"""

# AdSense placeholders (commented out until the site is approved and real
# slot IDs exist — never invent slot numbers).
AD_HOME_TOP = """<!-- AdSense: homepage top banner (uncomment after approval)
<ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-8912117199500932" data-ad-slot="YOUR_SLOT_ID" data-ad-format="auto" data-full-width-responsive="true"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>
-->"""

AD_ARTICLE_MID = """<!-- AdSense: article mid-page unit (uncomment after approval)
<ins class="adsbygoogle" style="display:block; text-align:center;" data-ad-layout="in-article" data-ad-format="fluid" data-ad-client="ca-pub-8912117199500932" data-ad-slot="YOUR_SLOT_ID"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>
-->"""

INDEX_BODY = """
<main class="wrap">
  <nav class="catnav" id="catnav">{cat_buttons}</nav>
  <div class="topbar">
    <div class="searchbox">
      <input type="search" id="q" placeholder="খবর খুঁজুন…" aria-label="খবর খুঁজুন">
      <button type="button" onclick="doSearch()">খুঁজুন</button>
    </div>
    <span class="updated">সর্বশেষ আপডেট: {updated}</span>
  </div>
  {ad_home}
  <div class="grid" id="grid">
{cards}
  </div>
  <div class="empty" id="noresult" style="display:none">দুঃখিত, কোনো খবর পাওয়া যায়নি।</div>
</main>
<script>
const btns = document.querySelectorAll('#catnav button');
const cards = document.querySelectorAll('#grid .card');
let cat = 'সর্বশেষ';
function applyFilter() {{
  const q = document.getElementById('q').value.trim();
  let visible = 0;
  cards.forEach(c => {{
    const okCat = (cat === 'সর্বশেষ') || c.dataset.cat === cat;
    const okQ = !q || c.textContent.includes(q);
    const show = okCat && okQ;
    c.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  document.getElementById('noresult').style.display = visible ? 'none' : '';
}}
btns.forEach(b => b.addEventListener('click', () => {{
  btns.forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  cat = b.dataset.cat;
  applyFilter();
}}));
function doSearch() {{ applyFilter(); }}
document.getElementById('q').addEventListener('input', applyFilter);
</script>
"""

CARD = """    <article class="card" data-cat="{cat}">
      <div class="meta"><span class="src">{source}</span><span class="cat">{cat}</span></div>
      <h2><a href="{root}news/{slug}.html">{title}</a></h2>
      <p>{summary}</p>
      <div class="meta"><span>{time}</span></div>
      <div class="links">
        <a class="btn detail" href="{root}news/{slug}.html">বিস্তারিত পড়ুন →</a>
        <a class="btn orig" href="{url}" target="_blank" rel="noopener">মূল সংবাদ ↗</a>
      </div>
    </article>
"""

ARTICLE_BODY = """
<main class="wrap">
  <a class="back" href="{root}">← সব খবর</a>
  <article class="article">
    <div class="meta"><span class="src">{source}</span><span class="cat">{cat}</span><span>{time}</span></div>
    <h1>{title}</h1>
    <p class="lede">{summary}</p>
    {ad_article}
    <div class="credit">
      <p><strong>উৎস:</strong> {source} — <a href="{url}" target="_blank" rel="noopener">মূল সংবাদ পড়ুন ↗</a></p>
      <p>এটি মূল ইংরেজি সংবাদের স্বয়ংক্রিয় বাংলা অনুবাদসহ সংক্ষিপ্তসার। পূর্ণাঙ্গ সংবাদটি উপরের লিংকে মূল প্রকাশকের সাইটে পড়ুন।</p>
    </div>
  </article>
</main>
<script type="application/ld+json">
{jsonld}
</script>
"""

NOT_FOUND = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>পাওয়া যায়নি | খবর দুনিয়া</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<header class="site-header"><div class="wrap">
<h1><a href="./">খবর দুনিয়া</a></h1>
<p class="tagline">বিশ্বের তাজা খবর, এখন বাংলায়</p>
</div></header>
<main class="wrap"><div class="empty">
<p>দুঃখিত, এই পাতাটি পাওয়া যায়নি — খবরটি হয়তো পুরনো হয়ে গেছে।</p>
<p><a href="./">← প্রথম পাতায় ফিরে যান</a></p>
</div></main>
</body>
</html>
"""

ROBOTS = """User-agent: *
Allow: /
Sitemap: {base}sitemap.xml
"""


def esc(s):
    # type: (str) -> str
    return html.escape(s or "", quote=True)


def write_file(path, content):
    # type: (Path, str) -> None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------- site generation
def generate_site(items, cache_stats):
    """Render the full static site into site/. Returns (pages, translated)."""
    translated = [it for it in items if it.get("title_bn")]
    now = datetime.now(timezone.utc)
    updated_bn = bn_datetime(now.isoformat())
    year = bn_num(now.astimezone(DHAKA).year)

    # copy assets
    for name in ("style.css", "config.js"):
        src = ASSETS_DIR / name
        if src.exists():
            write_file(SITE_DIR / "assets" / name, src.read_text(encoding="utf-8"))

    # category buttons with counts
    counts = {}
    for it in translated:
        counts[it["category"]] = counts.get(it["category"], 0) + 1
    buttons = []
    for c in CATEGORIES:
        n = len(translated) if c == "সর্বশেষ" else counts.get(c, 0)
        active = " class=\"active\"" if c == "সর্বশেষ" else ""
        buttons.append(
            f'<button type="button" data-cat="{c}"{active}>{c} <span class="n">({bn_num(n)})</span></button>')
    cat_buttons = "\n".join(buttons)

    # cards (newest first)
    cards = []
    for it in translated:
        cards.append(CARD.format(
            root="./", cat=it["category"], source=esc(it["source"]),
            slug=it["slug"], title=esc(it["title_bn"]),
            summary=esc(it.get("summary_bn") or ""),
            time=bn_datetime(it["published"]), url=esc(it["url"])))
    index_html = (
        PAGE_HEAD.format(title="বিশ্বের তাজা খবর বাংলায়",
                         meta_desc="বিশ্বের তাজা খবর বাংলায় — স্বয়ংক্রিয় সংবাদ সংগ্রাহক",
                         root="./", extra_head="") +
        INDEX_BODY.format(cat_buttons=cat_buttons, updated=updated_bn,
                          ad_home=AD_HOME_TOP, cards="\n".join(cards)) +
        PAGE_FOOT.format(year=year))
    write_file(SITE_DIR / "index.html", index_html)

    # article pages: translated items from the last ARTICLE_PAGE_DAYS days
    cutoff = now.timestamp() - ARTICLE_PAGE_DAYS * 86400
    pages = []
    for it in translated:
        pub = datetime.fromisoformat(it["published"]).timestamp()
        if pub < cutoff:
            continue
        jsonld = json.dumps({
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": it["title_bn"],
            "description": it.get("summary_bn") or it["title_bn"],
            "datePublished": it["published"],
            "inLanguage": "bn",
            "author": {"@type": "Organization", "name": it["source"]},
            "publisher": {"@type": "Organization", "name": "খবর দুনিয়া"},
            "mainEntityOfPage": BASE_URL + "news/" + it["slug"] + ".html",
        }, ensure_ascii=False, indent=2)
        art = (
            PAGE_HEAD.format(
                title=it["title_bn"][:70],
                meta_desc=(it.get("summary_bn") or it["title_bn"])[:150],
                root="../",
                extra_head=f'<link rel="canonical" href="{esc(it["url"])}">') +
            ARTICLE_BODY.format(
                root="../", source=esc(it["source"]), cat=it["category"],
                time=bn_datetime(it["published"]), title=esc(it["title_bn"]),
                summary=esc(it.get("summary_bn") or ""),
                ad_article=AD_ARTICLE_MID, url=esc(it["url"]), jsonld=jsonld) +
            PAGE_FOOT.format(year=year))
        write_file(SITE_DIR / "news" / (it["slug"] + ".html"), art)
        pages.append(it["slug"])

    # sitemap
    urls = [f"  <url><loc>{BASE_URL}</loc>"
            f"<lastmod>{now.date().isoformat()}</lastmod></url>"]
    for slug in pages:
        urls.append(
            f"  <url><loc>{BASE_URL}news/{slug}.html</loc>"
            f"<lastmod>{now.date().isoformat()}</lastmod></url>")
    write_file(SITE_DIR / "sitemap.xml",
               '<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' +
               "\n".join(urls) + "\n</urlset>\n")
    write_file(SITE_DIR / "robots.txt", ROBOTS.format(base=BASE_URL))
    write_file(SITE_DIR / "404.html", NOT_FOUND)
    return len(pages), len(translated)


# ---------------------------------------------------------------- main
def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    email = os.environ.get("MYMEMORY_EMAIL") or None
    budget = {"left": BUDGET_EMAIL if email else BUDGET_ANON}
    print(f"[info] translation budget this run: {budget['left']} chars "
          f"({'email tier' if email else 'anonymous tier'})")

    try:
        cache = json.loads(CACHE_JSON.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    try:
        old_items = json.loads(NEWS_JSON.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        old_items = []
    old_by_url = {it["url"]: it for it in old_items}

    # 1. fetch
    raw = []
    for source, url in FEEDS:
        raw.extend(parse_feed_xml(source, url))

    # 2. dedupe + merge with previous run's items
    merged = dict(old_by_url)
    for it in raw:
        if it["url"] in merged:
            # refresh evergreen fields, keep existing translations
            merged[it["url"]].update({k: it[k] for k in
                                      ("title_en", "summary_en", "source", "published")
                                      if it.get(k)})
        else:
            merged[it["url"]] = it
    items = sorted(merged.values(),
                   key=lambda x: x["published"], reverse=True)[:MAX_ITEMS]
    print(f"[info] {len(raw)} fetched, {len(items)} kept after dedupe")

    # 3. translate (newest first, so the freshest news wins the budget)
    new_translations = 0
    for it in items:
        for field in ("title_en", "summary_en"):
            bn_field = field.replace("_en", "_bn")
            if not it.get(field) or it.get(bn_field):
                continue
            t = translate_cached(it[field], cache, budget, email)
            if t:
                it[bn_field] = t
                new_translations += 1
            # on None: quota spent or API failed — item keeps English for now
    print(f"[info] new translations this run: {new_translations}, "
          f"budget left: {budget['left']}")

    # 4. classify + slug
    for it in items:
        it["category"] = classify(it["title_en"], it.get("summary_en", ""))
        it["slug"] = make_slug(it["title_en"], it["url"])
        it.setdefault("title_bn", None)
        it.setdefault("summary_bn", None)

    # 5. persist
    CACHE_JSON.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    NEWS_JSON.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                         encoding="utf-8")

    # 6. render
    pages, n_translated = generate_site(items, None)
    print(f"[done] {n_translated} translated items, {pages} article pages "
          f"-> {SITE_DIR}/")


if __name__ == "__main__":
    main()

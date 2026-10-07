# খবর দুনিয়া (Khobor Duniya)

A Bangla international news aggregator. Twice a day it collects world news
from free RSS feeds, machine-translates headlines + short summaries into
Bengali, and publishes a static site via GitHub Pages.

Live: https://mdnaimislambd.github.io/khobor-duniya/

## How it works

`builder/build.py` runs on a schedule (`.github/workflows/update.yml`,
cron `0 */12 * * *` — twice daily UTC):

1. **Fetch** — pulls RSS feeds from BBC World, Al Jazeera, DW, France 24,
   The Guardian, and NPR (all free, no API keys).
2. **Dedupe** — normalizes URLs and keeps the newest ~150 items.
3. **Translate** — sends each new headline + short summary to the free
   [MyMemory API](https://mymemory.translated.net/) (`en` → `bn`).
   Translations are cached in `data/translations.json` (keyed by SHA-1 of
   the English text), so an item is **never translated twice** and quota is
   only spent on genuinely new text.
4. **Classify** — simple keyword rules sort items into বিশ্ব / রাজনীতি /
   অর্থনীতি / প্রযুক্তি / খেলা / বিজ্ঞান.
5. **Render** — generates `index.html`, per-article pages (`news/*.html`,
   only for translated items from the last 7 days), `sitemap.xml`,
   `robots.txt`, and `404.html` into `site/`, which is deployed to Pages.
   The translation cache + `data/news.json` are committed back so the
   cache survives between runs.

Run it locally any time:

```bash
pip install -r builder/requirements.txt
python builder/build.py        # writes ./site/
```

## Legal model

This is an **aggregator**, not a re-publisher:

- Only headlines and short summaries are shown, machine-translated.
- Every card links prominently to the **original article** ("মূল সংবাদ পড়ুন")
  and every article page sets `<link rel="canonical">` to the original.
- The footer + article pages carry a Bangla disclaimer that these are
  automatic translations/summaries and full stories belong to the publishers.

Full article text is never copied.

## Translation quota

MyMemory's free tier allows roughly **5,000 characters/day anonymously**
and **~50,000/day** when a contact email is passed. The builder caps each
run accordingly (newest stories first). Items that miss the quota window
simply get their Bengali version on a later run — the cache makes this
progressive and free.

**To raise the quota:** add a repository secret named `MYMEMORY_EMAIL`
(Settings → Secrets and variables → Actions) containing any valid contact
email. The workflow passes it as `de` to MyMemory. No signup or key needed.

Rough math: one story ≈ 400–500 characters (title + summary).
Anonymous ≈ 10 stories/run ≈ 20/day · with email ≈ 100/run ≈ 200/day.

## Adding / removing feeds

Edit the `FEEDS` list at the top of `builder/build.py`
(`(source name, rss url)` pairs) and push — the `push` trigger rebuilds
the site automatically. (AP's feed was dropped on 2026-10-08: Cloudflare 403.)

## AdSense

`assets/config.js` holds the publisher ID `ca-pub-8912117199500932` and the
templates contain commented ad-unit placeholders — no slot IDs are invented
here. `ads.txt` is already served from the `mdnaimislambd.github.io` root.

Honest note: AdSense frequently rejects fully auto-generated aggregator
sites under its "low value content" policy. Adding original Bengali
writing (editorials, explainers, daily round-ups) materially improves the
odds of approval.

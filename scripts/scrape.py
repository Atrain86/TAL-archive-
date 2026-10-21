#!/usr/bin/env python3
"""Build/refresh episodes.json for the TAL Archive app.

First run: walks the whole episode archive and reads every episode page
(number, title, blurb, original air date, audio link).
Later runs: checks the newest archive pages and only reads episodes it
hasn't seen yet (plus a few retries for episodes missing audio).

Standard library only, so it runs on a stock GitHub Actions runner.
"""
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.thisamericanlife.org"
OUT = os.path.join(os.path.dirname(__file__), "..", "episodes.json")
UA = "Mozilla/5.0 (compatible; personal-episode-index; +https://github.com/Atrain86/tal-archive)"
PAUSE = 0.4  # be polite between requests

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(\d{1,2}),\s+(\d{4})\b"
)
ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def log(*a):
    print(*a, flush=True)


def fetch(url, tries=3):
    """Return (final_url, html) or (None, None) on 404/permanent failure."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", "replace")
                return r.geturl(), body
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None, None
            log(f"  HTTP {e.code} on {url} (try {i + 1})")
        except Exception as e:  # network hiccup
            log(f"  {type(e).__name__} on {url} (try {i + 1}): {e}")
        time.sleep(2 * (i + 1))
    return None, None


def text(s):
    return html.unescape(TAG_RE.sub(" ", s)).strip()


def meta(page, prop):
    m = re.search(
        r"""<meta[^>]+(?:property|name)\s*=\s*["']%s["'][^>]*content\s*=\s*["']([^"']*)["']""" % re.escape(prop),
        page, re.I)
    if not m:
        m = re.search(
            r"""<meta[^>]+content\s*=\s*["']([^"']*)["'][^>]*(?:property|name)\s*=\s*["']%s["']""" % re.escape(prop),
            page, re.I)
    return html.unescape(m.group(1)).strip() if m else ""


def absolute(u):
    u = html.unescape(u)
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return BASE + u
    return u


def parse_date(m):
    mon = MONTHS[m.group(1).lower()[:3]]
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


def days_between(a, b):
    da = datetime.strptime(a, "%Y-%m-%d")
    db = datetime.strptime(b, "%Y-%m-%d")
    return abs((da - db).days)


# ---------- discovery ----------

def listing_pairs(page_html):
    """Find (number, url) pairs: archive items link their episode number."""
    pairs = {}
    for attrs, inner in ANCHOR_RE.findall(page_html):
        label = text(inner)
        if not re.fullmatch(r"\d{1,4}", label):
            continue
        h = HREF_RE.search(attrs)
        if not h:
            continue
        url = absolute(h.group(1))
        if not url.startswith(BASE) or "/archive" in url:
            continue
        n = int(label)
        if 1 <= n <= 5000:
            pairs.setdefault(n, url)
    return pairs


def discover(full):
    found = {}
    max_pages = 60 if full else 3
    for p in range(max_pages):
        url = f"{BASE}/archive?type=episodes" + (f"&page={p}" if p else "")
        _, page = fetch(url)
        time.sleep(PAUSE)
        if not page:
            break
        pairs = listing_pairs(page)
        new = {n: u for n, u in pairs.items() if n not in found}
        found.update(new)
        log(f"archive page {p}: {len(pairs)} links, {len(new)} new (total {len(found)})")
        if not new:
            break
    return found


# ---------- episode page ----------

def parse_episode(n, final_url, page):
    title = meta(page, "og:title")
    title = re.sub(r"\s*[-|–]\s*This American Life\s*$", "", title).strip()
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.S | re.I)
    if not title and h1:
        title = text(h1.group(1))
    desc = meta(page, "og:description") or meta(page, "description")

    # Air date: header date shown just above the <h1>. If it looks like a
    # rerun date (far later than when the page was first published), use
    # the original publish date instead.
    header_date = None
    if h1:
        seg = page[max(0, h1.start() - 8000):h1.start()]
        ms = list(DATE_RE.finditer(text(seg)))
        if ms:
            header_date = parse_date(ms[-1])
    pub = meta(page, "article:published_time")[:10] or None
    date = header_date or pub
    if header_date and pub and pub < header_date and days_between(pub, header_date) > 90:
        date = pub

    # Audio: the "Download" link; never the beeped/clean version.
    mp3 = None
    candidates = []
    for attrs, inner in ANCHOR_RE.findall(page):
        h = HREF_RE.search(attrs)
        if not h or ".mp3" not in h.group(1).lower():
            continue
        url = absolute(h.group(1))
        if "/clean/" in url:
            continue
        score = 2 if "download" in text(inner).lower() or "download" in attrs.lower() else 1
        candidates.append((score, url))
    if candidates:
        candidates.sort(key=lambda c: -c[0])
        mp3 = candidates[0][1]
    else:
        for m in re.finditer(r"""https?:[^"'\s<>]+?\.mp3[^"'\s<>]*""", page):
            u = html.unescape(m.group(0))
            if "/clean/" not in u:
                mp3 = u
                break

    canon = ""
    m = re.search(r"""<link[^>]+rel\s*=\s*["']canonical["'][^>]*href\s*=\s*["']([^"']+)["']""", page, re.I)
    if m:
        canon = absolute(m.group(1))
    url = canon or meta(page, "og:url") or final_url

    return {"n": n, "t": title or f"Episode {n}", "d": date, "desc": desc, "url": url, "mp3": mp3}


def load_existing():
    try:
        with open(OUT, encoding="utf-8") as f:
            data = json.load(f)
        return {e["n"]: e for e in data.get("episodes", [])}
    except Exception:
        return {}


def main():
    existing = load_existing()
    full = len(existing) < 100 or "--full" in sys.argv
    log(f"{len(existing)} episodes on file; {'full' if full else 'incremental'} scan")

    found = discover(full)
    if not found and not existing:
        log("Could not read the archive listing at all.")
        sys.exit(1)

    # Fill numbering gaps by trying /<n> directly (oldest episodes, odd slugs).
    known = set(existing) | set(found)
    top = max(known) if known else 0
    gaps = [n for n in range(1, top + 1) if n not in known]
    if gaps and not full:
        gaps = gaps[-15:]  # incremental runs: only recent gaps
    todo = {n: u for n, u in found.items() if n not in existing}
    for n in gaps:
        todo.setdefault(n, f"{BASE}/{n}")
    # Retry a few entries that were saved without audio.
    retries = [n for n, e in existing.items() if not e.get("mp3")][:20]
    for n in retries:
        todo.setdefault(n, existing[n].get("url") or f"{BASE}/{n}")

    log(f"{len(todo)} episode pages to read")
    eps = dict(existing)
    misses = 0
    for i, (n, url) in enumerate(sorted(todo.items())):
        final, page = fetch(url)
        time.sleep(PAUSE)
        if not page:
            misses += 1
            continue
        ep = parse_episode(n, final, page)
        eps[n] = ep
        if i % 50 == 0 or not ep["mp3"] or not ep["d"]:
            log(f"#{n} {ep['t']!r} {ep['d']} audio={'yes' if ep['mp3'] else 'NO'}")

    episodes = sorted(eps.values(), key=lambda e: e["n"])
    with_audio = sum(1 for e in episodes if e.get("mp3"))
    no_date = sum(1 for e in episodes if not e.get("d"))
    warnings = []
    if episodes and with_audio / len(episodes) < 0.85:
        warnings.append(f"Only {with_audio} of {len(episodes)} episodes have playable audio here.")
    if no_date:
        warnings.append(f"{no_date} episodes are missing an air date.")

    data = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(episodes),
        "withAudio": with_audio,
        "warnings": warnings,
        "episodes": episodes,
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Saved {len(episodes)} episodes ({with_audio} with audio, {misses} pages not found).")
    for w in warnings:
        log("WARNING:", w)
    if len(episodes) < 50:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build/refresh the data behind the TAL Archive app.

Writes:
  episodes.json        the list: number, title, blurb, air date, audio, song search text
  details/<n>.json     per episode: acts (start time, length, blurb, credits, songs)
  collections.json     the site's curated lists (whole episodes or single acts)
  spotify-cache.json   song -> Spotify track lookups (only with Spotify keys)

First run (or a data-format upgrade) reads every episode page. Later runs
read only new episodes, refresh the collections, and look up any songs not
yet matched on Spotify.

Standard library only, so it runs on a stock GitHub Actions runner.
"""
import base64
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://www.thisamericanlife.org"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
OUT = os.path.join(ROOT, "episodes.json")
DETAILS = os.path.join(ROOT, "details")
COLLECTIONS_OUT = os.path.join(ROOT, "collections.json")
SPOTIFY_CACHE = os.path.join(ROOT, "spotify-cache.json")
UA = "Mozilla/5.0 (compatible; personal-episode-index; +https://github.com/Atrain86/tal-archive)"
PAUSE = 0.4  # be polite between requests
DATA_VERSION = 2
SPOTIFY_OK = False
SPOTIFY_STATS = {}

COLLECTIONS = [
    ("new-to-this-american-life", "New to This American Life"),
    ("staff-recommendations", "Staff Recommendations"),
    ("funny-stories", "Funny Stories"),
    ("award-winners", "Award Winners"),
    ("hour-long-stories", "Hour-Long Stories"),
    ("road-trip", "For Your Road Trip"),
    ("stories-kids-seem-to-like", "Stories Kids Seem to Like"),
]

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
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
                return r.geturl(), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None, None
            log(f"  HTTP {e.code} on {url} (try {i + 1})")
        except Exception as e:  # network hiccup
            log(f"  {type(e).__name__} on {url} (try {i + 1}): {e}")
        time.sleep(2 * (i + 1))
    return None, None


def text(s):
    s = re.sub(r"<br\s*/?>|</p>\s*<p[^>]*>", "\n", s or "")
    s = html.unescape(TAG_RE.sub(" ", s)).replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\s+([,.;:!?)])", r"\1", s)
    return s.strip()


def meta(page, prop):
    for pat in (
        r"""<meta[^>]+(?:property|name)\s*=\s*["']%s["'][^>]*content\s*=\s*["']([^"']*)["']""",
        r"""<meta[^>]+content\s*=\s*["']([^"']*)["'][^>]*(?:property|name)\s*=\s*["']%s["']""",
    ):
        m = re.search(pat % re.escape(prop), page, re.I)
        if m:
            return html.unescape(m.group(1)).strip()
    return ""


def absolute(u):
    u = html.unescape(u)
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("/"):
        return BASE + u
    return u


def path_of(u):
    return urllib.parse.urlparse(absolute(u)).path.rstrip("/") if u else ""


def parse_date(m):
    return f"{int(m.group(3)):04d}-{MONTHS[m.group(1).lower()[:3]]:02d}-{int(m.group(2)):02d}"


def days_between(a, b):
    return abs((datetime.strptime(a, "%Y-%m-%d") - datetime.strptime(b, "%Y-%m-%d")).days)


def is_promo_or_clean(u):
    """Skip the beeped version and short promo clips; only full episodes."""
    u = (u or "").lower()
    return "/clean/" in u or "/promos/" in u or "\\" in u


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


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
    for p in range(60 if full else 3):
        _, page = fetch(f"{BASE}/archive?type=episodes" + (f"&page={p}" if p else ""))
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


def fetch_episode(n, url):
    """Try the known URL, then the old-style /radio-archives/episode/<n>
    link (redirects to the real page for every era), then /<n>."""
    tried = []
    for u in (url, f"{BASE}/radio-archives/episode/{n}", f"{BASE}/{n}"):
        if not u or u in tried:
            continue
        tried.append(u)
        final, page = fetch(u)
        time.sleep(PAUSE)
        if page and "<h1" in page:
            return final, page
    return None, None


# ---------- episode page ----------

def split_song(raw):
    """'“Title” by Artist' -> (title, artist)."""
    s = raw.strip()
    m = re.match(r"""^[“"'‘](.+?)[”"'’]\s*(?:,?\s*by|—|–|-)\s+(.+)$""", s, re.S)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.match(r"^(.+?)\s+by\s+(.+)$", s)
    if m:
        return m.group(1).strip(" “”\""), m.group(2).strip()
    return s.strip(" “”\""), ""


def parse_acts(n, page):
    """Acts in page order: label/title, path, blurb, minutes, credits, songs."""
    acts = []
    starts = [m.start() for m in re.finditer(r'<article class="node node-act view-episode', page)]
    for i, s in enumerate(starts):
        end = page.find("</article>", s)
        chunk = page[s:end if end > 0 else len(page)]
        dm = re.search(r'data-episode="(\d+)"', chunk[:400])
        if dm and int(dm.group(1)) != n:
            continue
        a = {}
        h = re.search(r'<h2 class="act-header">\s*<a href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if h:
            a["u"] = path_of(h.group(1))
            a["name"] = text(h.group(2))
        lab = re.search(r'field-name-field-act-label.*?<div class="field-item[^"]*">(.*?)</div>', chunk, re.S)
        if lab:
            a["label"] = text(lab.group(1))
        body = re.search(r'field-name-body.*?<div class="field-item[^"]*">(.*?)</div></div></div>', chunk, re.S)
        if body:
            desc = text(body.group(1))
            mm = re.search(r"\((\d+)\s*minutes?\)", desc)
            if mm:
                a["min"] = int(mm.group(1))
                desc = (desc[:mm.start()] + desc[mm.end():]).strip()
            a["desc"] = re.sub(r"\s+\n", "\n", desc).strip()
        cred = re.search(r'field-name-field-contributor[^>]*>(.*?)(?=<div class="field-collection-container|<div class="field field-name-field-song|$)', chunk, re.S)
        if cred:
            ch = re.sub(r'</div>\s*<div class="field-item[^"]*">', ", ", cred.group(1))
            c = text(re.sub(r'<h2 class="field-label">\s*By\s*</h2>', "By ", ch))
            c = re.sub(r"\s*\n\s*", ", ", c).replace("By, ", "By ").strip(" ,")
            if c:
                a["credits"] = c
        songs = []
        sm = re.search(r"field-name-field-song.*", chunk, re.S)
        if sm:
            for item in re.findall(r'<div class="field-item[^"]*">(.*?)</div>', sm.group(0), re.S):
                raw = text(item)
                if not raw:
                    continue
                t, ar = split_song(raw)
                song = {"t": t, "a": ar}
                href = re.search(r'href="([^"]+)"', item)
                if href:
                    song["link"] = absolute(href.group(1))
                songs.append(song)
        if songs:
            a["songs"] = songs
        acts.append(a)
    return acts


def parse_episode(n, final_url, page):
    title = re.sub(r"\s*[-|–]\s*This American Life\s*$", "", meta(page, "og:title")).strip()
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.S | re.I)
    if not title and h1:
        title = text(h1.group(1))
    desc = meta(page, "og:description") or meta(page, "description")

    # Air date: header date shown just above the <h1>. If it looks like a
    # rerun date (far later than when the page was first published), use
    # the original publish date instead.
    header_date = None
    if h1:
        ms = list(DATE_RE.finditer(text(page[max(0, h1.start() - 8000):h1.start()])))
        if ms:
            header_date = parse_date(ms[-1])
    pub = meta(page, "article:published_time")[:10] or None
    date = header_date or pub
    if header_date and pub and pub < header_date and days_between(pub, header_date) > 90:
        date = pub

    # Player data: act start times plus audio. Prefer the plain archive
    # file (no inserted ads, so act start times line up).
    pl = {}
    m = re.search(r'<script id="playlist-data" type="application/json">(.*?)</script>', page, re.S)
    if m:
        try:
            pl = json.loads(m.group(1))
        except ValueError:
            pl = {}
    mp3 = None
    for cand in (pl.get("archive"), pl.get("audio")):
        if cand and ".mp3" in cand.lower() and not is_promo_or_clean(cand):
            mp3 = cand
            break
    if not mp3:  # fall back to the Download link
        best = []
        for attrs, inner in ANCHOR_RE.findall(page):
            h = HREF_RE.search(attrs)
            if not h or ".mp3" not in h.group(1).lower():
                continue
            u = absolute(h.group(1))
            if is_promo_or_clean(u):
                continue
            best.append((2 if "download" in (text(inner) + attrs).lower() else 1, u))
        if best:
            mp3 = sorted(best, key=lambda c: -c[0])[0][1]

    canon = re.search(r"""<link[^>]+rel\s*=\s*["']canonical["'][^>]*href\s*=\s*["']([^"']+)["']""", page, re.I)
    url = absolute(canon.group(1)) if canon else (meta(page, "og:url") or final_url)

    acts = parse_acts(n, page)
    times = pl.get("acts") or []
    if len(times) == len(acts):
        for a, t in zip(acts, times):
            if isinstance(t.get("timestamp"), (int, float)):
                a["at"] = int(t["timestamp"])
            nm = (t.get("name") or "").strip()
            if ": " in nm:  # "Act One: Some Title" -> keep the real title
                lab, ttl = nm.split(": ", 1)
                if ttl and ttl.lower() != lab.lower():
                    a["label"], a["title"] = lab, ttl
                else:
                    a["label"] = lab
    for a in acts:
        a.setdefault("label", a.get("name") or "")
        a.pop("name", None)

    songs = [s for a in acts for s in a.get("songs", [])]
    ep = {"n": n, "t": title or f"Episode {n}", "d": date, "desc": desc, "url": url, "mp3": mp3}
    if songs:
        ep["s"] = " · ".join(f"{s['t']} {s['a']}".strip() for s in songs)
    detail = {"n": n, "acts": acts}
    return ep, detail


# ---------- collections ----------

def scrape_collections():
    out = []
    for slug, fallback in COLLECTIONS:
        _, page = fetch(f"{BASE}/recommended/{slug}")
        time.sleep(PAUSE)
        if not page:
            log(f"collection {slug}: not found")
            continue
        h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.S)
        name = text(h1.group(1)) if h1 else fallback
        items = []
        for m in re.finditer(r'<article class="node node-(episode|act) view-collection[^"]*"[^>]*data-episode="(\d+)"', page):
            kind, n = m.group(1), int(m.group(2))
            chunk = page[m.start():page.find("</article>", m.start())]
            item = {"n": n}
            if kind == "act":
                h = re.search(r'<h3 class="act-header"><a href="([^"]+)"', chunk)
                if h:
                    item["act"] = path_of(h.group(1))
            items.append(item)
        log(f"collection {slug}: {len(items)} items")
        out.append({"id": slug, "name": name or fallback, "items": items})
    return out


# ---------- Spotify (exact track links) ----------

def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\s-\s.*$", " ", s)  # "Song - 2011 Remaster"
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\bthe\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def artist_parts(a):
    return [p for p in (norm(x) for x in re.split(r",|&|\band\b|\bfeat\.?|\bfeaturing\b|\bwith\b|/", a or "", flags=re.I)) if p]


class Spotify:
    def __init__(self, cid, secret):
        self.cid, self.secret, self.token, self.exp, self.why, self.blocked = cid, secret, None, 0, "", 0

    def auth(self):
        if self.token and time.time() < self.exp - 60:
            return True
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": "Basic " + base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode(),
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            self.token, self.exp = d["access_token"], time.time() + d.get("expires_in", 3600)
            return True
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
                self.why = f"{body.get('error', '')} {body.get('error_description', '')}".strip()
            except Exception:
                self.why = f"HTTP {e.code}"
            log(f"Spotify sign-in didn't work: {self.why}")
            return False
        except Exception as e:
            self.why = type(e).__name__
            log(f"Spotify sign-in didn't work: {e}")
            return False

    def search(self, q):
        for attempt in range(4):
            if self.blocked or not self.auth():
                return None
            url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
                {"q": q, "type": "track", "limit": 8, "market": "CA"})
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.load(r).get("tracks", {}).get("items", [])
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = int(e.headers.get("Retry-After", "5") or 5)
                    if wait > 30:  # long pause requested: stop for this run, resume next time
                        log(f"  Spotify asked to pause for {wait}s; stopping lookups for this run")
                        self.blocked = wait
                        return None
                    log(f"  Spotify asked to slow down; waiting {wait}s")
                    time.sleep(wait + 1)
                    continue
                if e.code == 401:
                    self.token = None
                    continue
                log(f"  Spotify HTTP {e.code} for {q!r}")
                return None
            except Exception as e:
                log(f"  Spotify {type(e).__name__}: {e}")
                time.sleep(3)
        return None

    def match(self, title, artist):
        nt, parts = norm(title), artist_parts(artist)
        if not nt:
            return ""
        queries = []
        if artist:
            queries.append(f'track:"{title}" artist:"{re.split(r",|&| and | feat", artist)[0].strip()}"')
            queries.append(f"{title} {artist}")
        else:
            queries.append(f'track:"{title}"')
        for q in queries:
            items = self.search(q)
            time.sleep(0.35)
            if items is None:
                return None  # couldn't ask; try again next run
            for it in items:
                tt = norm(it.get("name"))
                title_ok = tt == nt or (len(nt) > 3 and (nt in tt or tt in nt))
                names = [norm(a.get("name")) for a in it.get("artists", [])]
                artist_ok = not parts or any(p == n or (len(p) > 3 and (p in n or n in p)) for p in parts for n in names)
                if title_ok and artist_ok:
                    return it["id"]
        return ""


def add_spotify(details):
    global SPOTIFY_OK
    cid = re.sub(r"\s+", "", os.environ.get("SPOTIFY_CLIENT_ID") or "")
    secret = re.sub(r"\s+", "", os.environ.get("SPOTIFY_CLIENT_SECRET") or "")
    if not cid and not secret:
        log("No Spotify keys set: songs keep Spotify search links.")
        return None
    if not cid or not secret:
        missing = "SPOTIFY_CLIENT_SECRET" if cid else "SPOTIFY_CLIENT_ID"
        log(f"Only one Spotify key is set; {missing} is missing.")
        return f"Spotify setup needs one more step: the GitHub secret {missing} wasn't found — check its name."
    sp = Spotify(cid, secret)
    if not sp.auth():
        why = sp.why or "no reason given"
        shape = "" if (len(cid) == 32 and len(secret) == 32) else \
            f" (the keys are {len(cid)} and {len(secret)} characters; both should be 32)"
        return f"Spotify didn't accept the keys: {why}{shape}. Songs use search links until they're re-checked."
    SPOTIFY_OK = True
    cache = read_json(SPOTIFY_CACHE, {})
    today = datetime.now(timezone.utc).date()
    retry_before = (today - timedelta(days=90)).isoformat()
    deadline = time.time() + 20 * 60  # keep each update short; the rest continue next run
    looked = found = 0
    paused = False
    for n in sorted(details):
        d = details[n]
        changed = False
        for a in d.get("acts", []):
            for s in a.get("songs", []):
                key = f"{norm(s['t'])}|{norm(s['a'])}"
                hit = cache.get(key)
                need = hit is None or (not hit["id"] and hit["on"] < retry_before)
                if need and not paused:
                    if sp.blocked or time.time() > deadline:
                        paused = True
                    else:
                        tid = sp.match(s["t"], s["a"])
                        if tid is not None:
                            looked += 1
                            hit = cache[key] = {"id": tid, "on": today.isoformat()}
                            found += 1 if tid else 0
                            if looked % 100 == 0:
                                write_json(SPOTIFY_CACHE, cache)
                                log(f"  Spotify: {looked} songs looked up so far ({found} matched)")
                if hit and hit["id"] and s.get("sp") != hit["id"]:
                    s["sp"] = hit["id"]
                    changed = True
        if changed:
            d["_dirty"] = True
    write_json(SPOTIFY_CACHE, cache)
    keys = {f"{norm(s['t'])}|{norm(s['a'])}" for d in details.values() for a in d.get("acts", []) for s in a.get("songs", [])}
    SPOTIFY_STATS.update(
        songs=len(keys),
        matched=sum(1 for k in keys if cache.get(k, {}).get("id")),
        pending=sum(1 for k in keys if k not in cache),
    )
    log(f"Spotify: looked up {looked} songs this run ({found} matched); "
        f"{SPOTIFY_STATS['matched']} of {SPOTIFY_STATS['songs']} songs have exact links, {SPOTIFY_STATS['pending']} still to look up.")
    return None


# ---------- main ----------

def main():
    old = read_json(OUT, {})
    existing = {e["n"]: e for e in old.get("episodes", [])}
    gone = set(old.get("gone", []))
    os.makedirs(DETAILS, exist_ok=True)
    have_details = {int(f[:-5]) for f in os.listdir(DETAILS) if f.endswith(".json")}

    full = len(existing) < 100 or "--full" in sys.argv
    upgrade = old.get("v", 1) < DATA_VERSION
    if full:
        gone = set()
    log(f"{len(existing)} episodes on file; {'full' if full else 'incremental'} scan"
        + ("; upgrading data format (re-reading every episode page)" if upgrade else ""))

    found = discover(full)
    if not found and not existing:
        log("Could not read the archive listing at all.")
        sys.exit(1)

    known = set(existing) | set(found)
    top = max(known) if known else 0
    gaps = [n for n in range(1, top + 1) if n not in known and n not in gone]
    todo = {n: u for n, u in found.items() if n not in existing}
    for n in gaps:
        todo.setdefault(n, None)
    for n, e in existing.items():
        if upgrade or full or n not in have_details or not e.get("mp3"):
            todo.setdefault(n, e.get("url"))

    log(f"{len(todo)} episode pages to read ({len(gaps)} looked up by number)")
    eps = dict(existing)
    details = {}
    misses = 0
    for i, (n, url) in enumerate(sorted(todo.items())):
        final, page = fetch_episode(n, url)
        if not page:
            misses += 1
            if n not in eps:
                gone.add(n)
                log(f"#{n}: no page found")
            continue
        ep, det = parse_episode(n, final, page)
        eps[n] = ep
        det["_dirty"] = True
        details[n] = det
        if i % 50 == 0 or not ep["mp3"] or not det["acts"]:
            nsongs = sum(len(a.get("songs", [])) for a in det["acts"])
            log(f"#{n} {ep['t']!r} {ep['d']} audio={'yes' if ep['mp3'] else 'NO'} acts={len(det['acts'])} songs={nsongs}")

    # Load the rest so Spotify links can be added to episodes not re-read.
    for f in os.listdir(DETAILS):
        if f.endswith(".json"):
            n = int(f[:-5])
            if n not in details:
                details[n] = read_json(os.path.join(DETAILS, f), {"n": n, "acts": []})

    warnings = []
    sw = add_spotify(details)
    if sw:
        warnings.append(sw)

    for n, d in details.items():
        if d.pop("_dirty", False):
            write_json(os.path.join(DETAILS, f"{n}.json"), d)

    cols = scrape_collections()
    if len(cols) < len(COLLECTIONS):
        old_cols = {c["id"]: c for c in read_json(COLLECTIONS_OUT, [])}
        have = {c["id"] for c in cols}
        cols += [c for k, c in old_cols.items() if k not in have]
        if len(cols) < len(COLLECTIONS):
            warnings.append("Some curated lists couldn't be read this time.")
    write_json(COLLECTIONS_OUT, cols)

    episodes = sorted(eps.values(), key=lambda e: e["n"])
    with_audio = sum(1 for e in episodes if e.get("mp3"))
    if episodes and with_audio / len(episodes) < 0.85:
        warnings.append(f"Only {with_audio} of {len(episodes)} episodes have playable audio here.")
    no_date = sum(1 for e in episodes if not e.get("d"))
    if no_date:
        warnings.append(f"{no_date} episodes are missing an air date.")
    if len(gone) > 25:
        warnings.append(f"{len(gone)} episode numbers couldn't be found on the site.")
    no_acts = sum(1 for n in eps if not details.get(n, {}).get("acts"))
    if no_acts > 30:
        warnings.append(f"{no_acts} episodes are missing act details.")

    write_json(OUT, {
        "v": DATA_VERSION,
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(episodes),
        "withAudio": with_audio,
        "spotify": SPOTIFY_OK,
        "spotifyStats": SPOTIFY_STATS,
        "warnings": warnings,
        "gone": sorted(gone),
        "episodes": episodes,
    })
    log(f"Saved {len(episodes)} episodes ({with_audio} with audio, {misses} pages not found).")
    for w in warnings:
        log("WARNING:", w)
    if len(episodes) < 50:
        sys.exit(1)


if __name__ == "__main__":
    main()

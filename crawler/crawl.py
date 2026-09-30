#!/usr/bin/env python3
"""
PhD Board crawler.

Runs every 4 hours from GitHub Actions. For each configured source it fetches search
result pages (respecting robots.txt and a per-domain delay), extracts candidate links,
keeps only those that look like PhD openings in the configured research areas, and
merges them into data/positions.json. Nothing is ever deleted: listings whose deadline
has passed, or that have not been seen for `expire_after_days_unseen`, become "expired"
and show up in the site's Archive tab.

Outputs
  data/positions.json       all crawled listings (open + expired)
  data/social_handles.json  X accounts that posted relevant openings (X API only)
  data/meta.json            run time + per-source health, shown on the site
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.robotparser

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dparser

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CFG = yaml.safe_load((ROOT / "crawler" / "config.yaml").read_text(encoding="utf-8"))

UA = CFG["user_agent"]
DELAY = float(CFG.get("request_delay_seconds", 3))
TIMEOUT = float(CFG.get("timeout_seconds", 25))
NOW = dt.datetime.now(dt.timezone.utc)
TODAY = NOW.date()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "en;q=0.9"})

# ───────────────────────────── text matching ─────────────────────────────

def terms_rx(terms):
    """Whole-word, case-insensitive matcher (short tokens like 'sar' won't hit 'sarah')."""
    alts = sorted({re.escape(t.lower()) for t in terms}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])")


AREAS = {
    name: (terms_rx(spec["terms"]),
           terms_rx(spec["context_required"]) if spec.get("context_required") else None,
           spec["terms"])
    for name, spec in CFG["research_areas"].items()
}
POS_RX = terms_rx(CFG["position_keywords"])
POSTDOC_RX = terms_rx(CFG["postdoc_keywords"])
SCHOL_RX = terms_rx(CFG["scholarship_keywords"])

COUNTRIES = {
    # canonical: aliases (lowercase)
    "USA": ["usa", "united states", "u.s.", "u.s.a"],
    "Canada": ["canada"],
    "UK": ["united kingdom", "england", "scotland", "wales", "northern ireland", "london", "edinburgh", "oxford", "cambridge, uk"],
    "Ireland": ["ireland", "dublin"],
    "Germany": ["germany", "deutschland", "munich", "münchen", "berlin", "stuttgart", "bonn", "hannover", "karlsruhe", "dresden", "aachen"],
    "Netherlands": ["netherlands", "the netherlands", "delft", "enschede", "wageningen", "eindhoven", "amsterdam"],
    "Switzerland": ["switzerland", "zurich", "zürich", "lausanne", "bern", "geneva"],
    "Austria": ["austria", "vienna", "wien", "graz", "innsbruck"],
    "Belgium": ["belgium", "leuven", "ghent", "brussels"],
    "France": ["france", "paris", "toulouse", "grenoble", "lyon"],
    "Italy": ["italy", "milan", "milano", "rome", "turin", "torino", "trento"],
    "Spain": ["spain", "madrid", "barcelona", "valencia"],
    "Portugal": ["portugal", "lisbon", "porto"],
    "Denmark": ["denmark", "copenhagen", "aarhus", "aalborg"],
    "Sweden": ["sweden", "stockholm", "gothenburg", "lund", "uppsala"],
    "Norway": ["norway", "oslo", "trondheim", "bergen"],
    "Finland": ["finland", "helsinki", "espoo", "tampere"],
    "Poland": ["poland", "warsaw", "krakow", "kraków"],
    "Czech Republic": ["czech republic", "czechia", "prague"],
    "Estonia": ["estonia", "tartu", "tallinn"],
    "Greece": ["greece", "athens"],
    "Japan": ["japan", "tokyo", "kyoto", "osaka", "tohoku", "sendai"],
    "China": ["china", "beijing", "shanghai", "wuhan", "shenzhen", "nanjing", "hangzhou"],
    "Hong Kong": ["hong kong"],
    "Taiwan": ["taiwan", "taipei"],
    "South Korea": ["south korea", "korea", "seoul", "daejeon", "kaist"],
    "Singapore": ["singapore"],
    "Australia": ["australia", "sydney", "melbourne", "brisbane", "perth", "canberra", "adelaide"],
    "New Zealand": ["new zealand", "auckland", "wellington", "christchurch"],
}
COUNTRY_RX = [(c, terms_rx(a)) for c, a in COUNTRIES.items()]
TLD = {"de": "Germany", "uk": "UK", "nl": "Netherlands", "ch": "Switzerland", "at": "Austria",
       "be": "Belgium", "fr": "France", "it": "Italy", "es": "Spain", "pt": "Portugal",
       "dk": "Denmark", "se": "Sweden", "no": "Norway", "fi": "Finland", "pl": "Poland",
       "cz": "Czech Republic", "ee": "Estonia", "gr": "Greece", "ie": "Ireland", "jp": "Japan",
       "cn": "China", "hk": "Hong Kong", "tw": "Taiwan", "kr": "South Korea", "sg": "Singapore",
       "au": "Australia", "nz": "New Zealand", "ca": "Canada", "edu": "USA"}
REGION = {
    "USA": "North America", "Canada": "North America",
    "Japan": "East Asia", "China": "East Asia", "Hong Kong": "East Asia", "Taiwan": "East Asia",
    "South Korea": "East Asia", "Singapore": "Southeast Asia",
    "Australia": "Oceania", "New Zealand": "Oceania",
}


def region_of(country):
    if not country:
        return ""
    return REGION.get(country, "Europe")


# ───────────────────────────── networking ─────────────────────────────

_robots: dict[str, urllib.robotparser.RobotFileParser] = {}
_last_hit: dict[str, float] = {}


def _host(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def robots_allows(url):
    parts = urllib.parse.urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    rp = _robots.get(base)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = SESSION.get(base + "/robots.txt", timeout=TIMEOUT)
            if r.status_code in (401, 403):
                rp.disallow_all = True           # same convention as the stdlib
            elif r.status_code >= 400:
                rp.allow_all = True
            else:
                rp.parse(r.text.splitlines())
        except requests.RequestException:
            rp.allow_all = True
        rp.modified()
        _robots[base] = rp
    return rp.can_fetch(UA, url)


def fetch(url):
    """GET with per-domain politeness delay. Raises on failure."""
    if not robots_allows(url):
        raise PermissionError("disallowed by robots.txt")
    host = _host(url)
    wait = DELAY - (time.monotonic() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()
    r = SESSION.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r


# ───────────────────────────── extraction ─────────────────────────────

_TWO_PART_SLD = {"co", "ac", "edu", "gov", "org", "com", "net"}


def site_of(url):
    labels = _host(url).split(":")[0].split(".")
    if len(labels) >= 3 and labels[-2] in _TWO_PART_SLD and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


_TRACKING = re.compile(r"^(utm_|ref$|trk|source$|fbclid$|gclid$|mc_)", re.I)


def norm_url(url):
    p = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not _TRACKING.match(k)]
    return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"),
                                    urllib.parse.urlencode(q), ""))


def clean(text):
    return " ".join((text or "").split())


def context_text(tag):
    """Text of the nearest ancestor that looks like a listing card."""
    node = tag
    best = clean(tag.get_text(" "))
    for _ in range(6):
        node = node.parent
        if node is None or node.name in ("body", "html", "[document]"):
            break
        txt = clean(node.get_text(" "))
        if len(txt) > 1500:
            break
        best = txt
        if len(txt) >= 180:
            break
    return best[:1200]


def extract_html(html, base_url, same_site=True):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "form", "svg"]):
        t.decompose()
    base_site = site_of(base_url)
    seen = set()
    for a in soup.find_all("a", href=True):
        title = clean(a.get_text(" "))
        if not 12 <= len(title) <= 220:
            continue
        href = urllib.parse.urljoin(base_url, a["href"]).split("#")[0]
        if not href.startswith(("http://", "https://")):
            continue
        if same_site and site_of(href) != base_site:
            continue
        if norm_url(href) == norm_url(base_url):
            continue
        key = norm_url(href)
        if key in seen:
            continue
        seen.add(key)
        yield title, href, context_text(a)


def extract_rss(content):
    feed = feedparser.parse(content)
    for e in feed.entries:
        title = clean(BeautifulSoup(e.get("title", ""), "html.parser").get_text(" "))
        summary = clean(BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" "))
        link = e.get("link", "")
        # Google Alerts wraps links: https://www.google.com/url?...&url=REAL
        if "google.com/url" in link:
            link = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("url", [link])[0]
        if title and link:
            yield title, link, summary[:1200]


# ───────────────────────────── classification ─────────────────────────────

DEADLINE_RX = re.compile(
    r"(?:deadline|closing date|closes|close date|apply by|apply before|applications? (?:close|due)|expires?)"
    r"[\s:\-–]*(?:on\s+|is\s+)?"
    r"(\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9},?\s+20\d\d"
    r"|[A-Za-z]{3,9}\s+\d{1,2}(?:st|nd|rd|th)?,?\s+20\d\d"
    r"|20\d\d-\d\d-\d\d"
    r"|\d{1,2}[./]\d{1,2}[./]20\d\d)", re.I)

INST_RX = re.compile(
    r"((?:University|Universit[äa]t|Universit[ée]|Universidad|Universit[àa]|Technical University|"
    r"Institute|Institut|Polytechnic|Politecnico|Academy|Centre|Center|College)"
    r"(?: of| for| f[üu]r| de| di)?(?: [A-Z][\w&'.\-]*){1,6}"
    r"|(?:[A-Z][\w&'.\-]*\s){1,4}(?:University|Institute of Technology|Institute))")


def parse_deadline(text):
    m = DEADLINE_RX.search(text)
    if not m:
        return None, ""
    raw = m.group(1)
    try:
        d = dparser.parse(raw, dayfirst=bool(re.match(r"\d{1,2}[./]", raw)), fuzzy=True).date()
        if 2023 <= d.year <= 2030:
            return d.isoformat(), raw
    except (ValueError, OverflowError):
        pass
    return None, raw


def detect_country(text, link, source_url):
    low = text.lower()
    for country, rx in COUNTRY_RX:
        if rx.search(low):
            return country
    if site_of(link) != site_of(source_url):      # direct university link → trust its TLD
        tld = _host(link).rsplit(".", 1)[-1]
        return TLD.get(tld, "")
    return ""


def classify(title, ctx, source):
    """Return dict of classification fields, or None if not a relevant PhD listing."""
    text = f"{title} {ctx}".lower()
    tlow = title.lower()
    areas, score = [], 0
    for name, (rx, ctx_rx, _) in AREAS.items():
        hits = rx.findall(text)
        if not hits:
            continue
        if ctx_rx is not None and not ctx_rx.search(text):
            continue
        areas.append(name)
        score += len(hits) + (3 if rx.search(tlow) else 0)
    if not areas:
        return None

    is_postdoc = bool(POSTDOC_RX.search(tlow))
    is_phd = bool(POS_RX.search(text))
    if is_postdoc and not POS_RX.search(tlow):
        if not CFG.get("include_postdoc"):
            return None
        kind = "Postdoc"
    elif is_phd or source.get("phd_only"):
        kind = "PhD Scholarship" if SCHOL_RX.search(tlow) else "PhD Position"
    else:
        return None
    return {"areas": areas, "type": kind, "relevance": score}


def build_item(title, link, ctx, source, source_url, seen_date=None):
    c = classify(title, ctx, source)
    if not c:
        return None
    deadline, deadline_text = parse_deadline(f"{title} {ctx}")
    inst = INST_RX.search(f"{title} {ctx}")
    country = detect_country(f"{title} {ctx}", link, source_url)
    when = (seen_date or NOW).isoformat(timespec="seconds")
    return {
        "id": hashlib.sha1(norm_url(link).encode()).hexdigest()[:12],
        "title": title,
        "url": link,
        "source": source["name"],
        "snippet": ctx[:500],
        "institution": clean(inst.group(1))[:90] if inst else "",
        "country": country,
        "region": region_of(country),
        "areas": c["areas"],
        "type": c["type"],
        "relevance": c["relevance"],
        "deadline": deadline,
        "deadline_text": deadline_text,
        "first_seen": when,
        "last_seen": when,
        "status": "open",
        "origin": "crawler",
    }


# ───────────────────────────── source runners ─────────────────────────────

def urls_for(source):
    if "{q}" in source["url"]:
        return [source["url"].replace("{q}", urllib.parse.quote(q)) for q in CFG["queries"]]
    return [source["url"]]


def run_source(source):
    found, errors = {}, []
    cap = int(CFG.get("max_items_per_page", 60))
    for url in urls_for(source):
        try:
            resp = fetch(url)
        except Exception as e:                                   # noqa: BLE001
            errors.append(f"{type(e).__name__}: {str(e)[:120]}")
            if isinstance(e, PermissionError):
                break                                            # robots says no → stop this source
            continue
        if source["type"] == "rss":
            candidates = extract_rss(resp.content)
        else:
            candidates = extract_html(resp.text, url, source.get("same_site", True))
        for i, (title, link, ctx) in enumerate(candidates):
            if i >= cap * 4:
                break
            item = build_item(title, link, ctx, source, url)
            if item:
                found[item["id"]] = item
    return list(found.values()), errors


def run_x(meta):
    token = os.environ.get("X_BEARER_TOKEN", "").strip()
    xc = CFG.get("x_api") or {}
    if not token:
        return [], [], "skipped (no X_BEARER_TOKEN secret)"
    last = meta.get("last_x_run")
    if last:
        hours = (NOW - dt.datetime.fromisoformat(last)).total_seconds() / 3600
        if hours < float(xc.get("min_interval_hours", 12)):
            return [], [], f"skipped (last run {hours:.1f} h ago)"
    params = {
        "query": " ".join(xc["query"].split()),
        "max_results": min(int(xc.get("max_results", 100)), 100),
        "tweet.fields": "created_at,author_id",
        "expansions": "author_id",
        "user.fields": "username,name,description,url",
    }
    r = requests.get("https://api.x.com/2/tweets/search/recent", params=params,
                     headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT)
    if r.status_code != 200:
        return [], [], f"error HTTP {r.status_code}: {r.text[:150]}"
    payload = r.json()
    users = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
    src = {"name": "X", "phd_only": False}
    items, handles = [], []
    for t in payload.get("data", []):
        u = users.get(t["author_id"], {})
        username = u.get("username", "i")
        link = f"https://x.com/{username}/status/{t['id']}"
        text = clean(t["text"])
        ctx = f"{text} {u.get('description', '')}"
        item = build_item(text[:180], link, ctx, src, "https://x.com",
                          dt.datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")))
        if not item:
            continue
        item["institution"] = item["institution"] or clean(u.get("description", ""))[:90]
        items.append(item)
        handles.append({"platform": "X", "handle": username, "name": u.get("name", ""),
                        "bio": clean(u.get("description", ""))[:200], "areas": item["areas"],
                        "last_post": link, "last_seen": item["first_seen"]})
    meta["last_x_run"] = NOW.isoformat(timespec="seconds")
    return items, handles, f"ok ({len(items)} relevant of {len(payload.get('data', []))})"


# ───────────────────────────── merge / persist ─────────────────────────────

def load_json(name, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"WARNING: {name} is corrupt; starting fresh", file=sys.stderr)
    return default


def save_json(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def merge(existing, new_items):
    """Returns number of genuinely new listings."""
    added = 0
    for it in new_items:
        old = existing.get(it["id"])
        if old is None:
            existing[it["id"]] = it
            added += 1
        else:
            old["last_seen"] = max(old["last_seen"], it["last_seen"])
            old["first_seen"] = min(old["first_seen"], it["first_seen"])
            for k in ("deadline", "deadline_text", "institution", "country", "region"):
                if not old.get(k) and it.get(k):
                    old[k] = it[k]
            old["areas"] = sorted(set(old["areas"]) | set(it["areas"]))
    return added


def refresh_status(items):
    horizon = float(CFG.get("expire_after_days_unseen", 60))
    for it in items.values():
        expired = False
        if it.get("deadline"):
            expired = dt.date.fromisoformat(it["deadline"]) < TODAY
        seen = dt.datetime.fromisoformat(it["last_seen"])
        if (NOW - seen).days > horizon:
            expired = True
        it["status"] = "expired" if expired else "open"


def merge_handles(new_handles):
    handles = {(h["platform"], h["handle"].lower()): h for h in load_json("social_handles.json", [])}
    for h in new_handles:
        key = (h["platform"], h["handle"].lower())
        old = handles.get(key)
        if old:
            old.update(h)
            old["posts"] = old.get("posts", 1) + 1
        else:
            h["posts"] = 1
            handles[key] = h
    save_json("social_handles.json", sorted(handles.values(), key=lambda h: h["last_seen"], reverse=True))


def main():
    DATA.mkdir(exist_ok=True)
    meta = load_json("meta.json", {})
    existing = {it["id"]: it for it in load_json("positions.json", [])}
    health = []

    for source in CFG.get("sources", []):
        t0 = time.monotonic()
        items, errors = run_source(source)
        added = merge(existing, items)
        status = "ok" if items or not errors else "error"
        health.append({"name": source["name"], "status": status, "found": len(items), "new": added,
                       "errors": errors[:3], "seconds": round(time.monotonic() - t0, 1)})
        print(f"[{source['name']}] found={len(items)} new={added} errors={len(errors)}")

    try:
        x_items, x_handles, x_status = run_x(meta)
    except Exception as e:                                       # noqa: BLE001
        x_items, x_handles, x_status = [], [], f"error {type(e).__name__}: {e}"
    x_added = merge(existing, x_items)
    if x_handles:
        merge_handles(x_handles)
    elif not (DATA / "social_handles.json").exists():
        save_json("social_handles.json", [])
    health.append({"name": "X (official API)", "status": x_status.split(" ")[0], "found": len(x_items),
                   "new": x_added, "errors": [] if x_status.startswith(("ok", "skipped")) else [x_status],
                   "note": x_status})

    refresh_status(existing)
    ordered = sorted(existing.values(), key=lambda i: i["first_seen"], reverse=True)
    save_json("positions.json", ordered)
    meta.update({
        "last_run": NOW.isoformat(timespec="seconds"),
        "sources": health,
        "totals": {"all": len(ordered), "open": sum(i["status"] == "open" for i in ordered),
                   "new_this_run": sum(h["new"] for h in health)},
    })
    save_json("meta.json", meta)
    print(f"Done: {meta['totals']}")


if __name__ == "__main__":
    main()

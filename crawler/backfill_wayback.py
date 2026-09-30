#!/usr/bin/env python3
"""
One-off backfill of 2024–2026 listings from the Internet Archive (Wayback Machine).

    python crawler/backfill_wayback.py            # all configured search URLs
    python crawler/backfill_wayback.py --from 2024 --to 2025 --per-url 6

Be realistic about yield: the Wayback Machine only has a page if someone archived that
exact URL. Keyword search pages are archived far less often than home pages, so expect
tens of historical listings, not thousands. Recovered items are marked origin="wayback";
their original links are often dead, so each also carries an `archived_url`.
"""
import argparse
import datetime as dt
import sys
import time
import urllib.parse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import crawl  # noqa: E402

CDX = "https://web.archive.org/cdx/search/cdx"


def snapshots(url, year_from, year_to, limit):
    params = {"url": url, "from": str(year_from), "to": str(year_to), "output": "json",
              "filter": "statuscode:200", "collapse": "timestamp:6", "limit": str(limit)}
    r = crawl.SESSION.get(CDX, params=params, timeout=60)
    r.raise_for_status()
    rows = r.json()
    return [(row[1], row[2]) for row in rows[1:]] if rows else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="yfrom", type=int, default=2024)
    ap.add_argument("--to", dest="yto", type=int, default=2026)
    ap.add_argument("--per-url", type=int, default=12, help="max snapshots per URL (≈ one per month)")
    args = ap.parse_args()

    targets = []
    for src in crawl.CFG.get("sources", []):
        if src["type"] in ("html_search", "html_page"):
            targets += [(src, u) for u in crawl.urls_for(src)]
    targets += [({"name": "Backfill", "same_site": False}, u) for u in crawl.CFG.get("backfill_urls", [])]

    existing = {it["id"]: it for it in crawl.load_json("positions.json", [])}
    total_new = 0
    for src, url in targets:
        try:
            snaps = snapshots(url, args.yfrom, args.yto, args.per_url)
        except Exception as e:                                  # noqa: BLE001
            print(f"CDX failed for {url}: {e}")
            continue
        for ts, original in snaps:
            time.sleep(1.5)
            try:
                r = crawl.SESSION.get(f"https://web.archive.org/web/{ts}id_/{original}", timeout=60)
                r.raise_for_status()
            except Exception as e:                              # noqa: BLE001
                print(f"  snapshot {ts} failed: {e}")
                continue
            when = dt.datetime.strptime(ts[:14], "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
            items = []
            for title, link, ctx in crawl.extract_html(r.text, original, src.get("same_site", True)):
                it = crawl.build_item(title, link, ctx, {**src, "name": f"{src['name']} (archived)"}, original, when)
                if it:
                    it["origin"] = "wayback"
                    it["archived_url"] = f"https://web.archive.org/web/{ts}/{urllib.parse.quote(link, safe=':/?&=%')}"
                    items.append(it)
            n = crawl.merge(existing, items)
            total_new += n
            print(f"{src['name']} {ts[:8]}: {len(items)} relevant, {n} new")
    crawl.refresh_status(existing)
    crawl.save_json("positions.json", sorted(existing.values(), key=lambda i: i["first_seen"], reverse=True))
    print(f"Backfill done: {total_new} new historical listings")


if __name__ == "__main__":
    main()

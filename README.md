# PhD Board

A job board for geospatial PhD openings in LiDAR, SLAM, photogrammetry, remote sensing and GeoAI. It is a static site on Netlify with a GitHub Actions crawler that runs every 4 hours. Your Excel workbook is the manual data source.

```
index.html                     the website (no build step)
data/tracker.xlsx              ← YOU EDIT THIS (Scholarships, Positions, Professors, Labs)
data/*.json                    generated; the site reads these
crawler/config.yaml            sources, search terms, research-area keywords
crawler/crawl.py               crawler (runs every 4 h)
crawler/excel_to_json.py       tracker.xlsx → JSON
crawler/backfill_wayback.py    optional one-off 2024–2026 history from the Wayback Machine
.github/workflows/update-data.yml
netlify.toml
```

## Setup (about 15 minutes)

1. **Create a GitHub repo.** Make it **private**: the workbook holds your personal "My Applications" sheet. Push everything in this folder to `main`.
2. **Edit `crawler/config.yaml`.** Put your contact email in `user_agent`. Responsible crawlers identify themselves.
3. **Allow the Action to push.** Go to *Settings → Actions → General → Workflow permissions* and select **Read and write**.
4. **Run it once.** Go to *Actions → Update PhD data → Run workflow*.
5. **Deploy on Netlify.** Choose *Add new site → Import from Git*, pick the repo, leave the build command empty, and set the publish directory to `.`.
   Each data commit triggers a redeploy, about 6 per day. That fits well within Netlify's free build allowance.
6. **Optional: X.** Add a repository secret `X_BEARER_TOKEN` under *Settings → Secrets → Actions*. The X API is paid, so check current pricing before you enable it.

## Adding data

- **From your laptop:** edit `data/tracker.xlsx` and upload it to the same path. You can use GitHub's web UI (*Add file → Upload files*) or `git push`. The Action converts it within about a minute.
- **Research Areas** must use the names listed on the workbook's *How to use* sheet, separated by `;`.
- **Department career pages:** add them to `sources:` in `config.yaml` with `type: html_page`.
- **Google Alerts** is the compliant way to catch posts from LinkedIn and the wider web. Create an alert (for example `"PhD position" LiDAR`), set *Deliver to* to **RSS feed**, and add the feed URL as a `type: rss` source.

## What it deliberately does not do

- **No LinkedIn scraping.** It violates LinkedIn's terms of service, it is actively blocked, and there is no public jobs API. Use Google Alerts RSS, or copy LinkedIn posts into the Positions sheet.
- **No fabricated history.** The archive fills up from the first crawl onward. `backfill_wayback.py` recovers some 2024–2025 pages, but expect tens of listings, not thousands.
- **No guessed social handles.** Professor handles come from your sheet, or from X accounts that actually posted relevant openings (X API only). Otherwise the site shows a search link.
- **Respects `robots.txt`.** If a board disallows bots, it is skipped and shown as an error under *Source health*. That is by design.

## Maintenance you should expect

- **Board redesigns.** Job boards change their HTML. The extractor is layout-agnostic: it reads every link plus the text around it. Still, check *Source health* in the site footer every week or two.
- **False positives and negatives.** Relevance is keyword-based. Tune `research_areas` and `context_required` in `config.yaml`.
- **Cron pauses.** GitHub disables scheduled workflows in repos with no activity for 60 days. The bot's own data commits normally keep the repo active.
- **Actions minutes on a private repo.** Private repos get 2,000 free minutes a month. This workflow uses roughly 5–8 minutes per run, which is about 1,000–1,400 minutes a month. Lower the frequency or the number of queries if you approach the limit.

## Local test

```bash
pip install -r crawler/requirements.txt
python crawler/excel_to_json.py && python crawler/crawl.py
python -m http.server 8000     # open http://localhost:8000
```

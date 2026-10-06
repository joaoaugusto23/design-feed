#!/usr/bin/env python3
"""
Daily search: looks for design jobs posted in the last 24 hours on the hiring systems
the watcher reads, using the Brave Search API. Any company it finds that isn't
already watched is added to discovered.txt, so the watcher starts checking it and
emails you its fresh roles.

Runs twice a day on GitHub (see .github/workflows/daily-search.yml).
Needs a BRAVE_API_KEY secret. Without one it does nothing.

Cost safety: Brave gives about 1,000 free searches a month. This script uses
about 600 and stops for the month at MONTHLY_LIMIT, so it stays inside the free credit.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import watcher
import discover

ROOT = watcher.ROOT
USAGE_FILE = ROOT / "search_usage.json"
OUT_FILE = discover.OUT_FILE
MONTHLY_LIMIT = 800

TITLES = '("product designer" OR "ux designer" OR "ui designer" OR "ux/ui designer")'
SITES = [
    "job-boards.greenhouse.io", "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
    "myworkdayjobs.com", "jobs.smartrecruiters.com", "apply.workable.com",
    "breezy.hr", "bamboohr.com", "recruitee.com",
]
HEADER = ("# Companies found automatically by discover.py (runs weekly).\n"
          "# Don't edit by hand; put your own picks in companies.txt instead.\n"
          "# Companies whose job pages disappear are skipped by the watcher automatically.\n")


def load_usage():
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        usage = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        usage = {}
    if usage.get("month") != month:
        usage = {"month": month, "searches": 0}
    return usage


def brave_search(key, query):
    params = urllib.parse.urlencode({"q": query, "count": 20, "freshness": "pd", "country": "US"})
    req = urllib.request.Request(f"https://api.search.brave.com/res/v1/web/search?{params}",
                                 headers={"Accept": "application/json", "X-Subscription-Token": key})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    return [res.get("url", "") for res in (data.get("web") or {}).get("results", [])]


def main():
    key = os.environ.get("BRAVE_API_KEY", "").strip()
    if not key:
        print("No BRAVE_API_KEY secret yet, so there's nothing to do. (This isn't an error.)")
        return
    usage = load_usage()

    found = set()
    for site in SITES:
        if usage["searches"] >= MONTHLY_LIMIT:
            print(f"Reached this month's limit of {MONTHLY_LIMIT} searches; stopping to stay within the free credit.")
            break
        query = f"{TITLES} remote site:{site}"
        try:
            urls = brave_search(key, query)
        except Exception as e:
            print(f"{site}: search failed ({e})")
            urls = []
        usage["searches"] += 1
        for url in urls:
            c = watcher.parse_company(url)
            if c:
                found.add(c)
        print(f"{site}: {len(urls)} results")
        time.sleep(1.2)  # the free plan allows about one search per second
    USAGE_FILE.write_text(json.dumps(usage, indent=1) + "\n", encoding="utf-8")
    print(f"Searches used this month: {usage['searches']} of {MONTHLY_LIMIT}.")

    known = []
    watcher.read_company_file(ROOT / "companies.txt", "manual", set(), known, [])
    watcher.read_company_file(OUT_FILE, "discovered", set(), known, [])
    known_keys = {(a, s.lower()) for a, s, _ in known}
    candidates = []
    for ats, slug in sorted(found):
        if ats in discover.LOWERCASE_NAMES:
            slug = slug.lower()
        if (ats, slug.lower()) not in known_keys:
            candidates.append((ats, slug))
    print(f"{len(found)} companies in today's results; {len(candidates)} not watched yet.")

    added = [c for c, ok in map(discover.worth_watching, candidates) if ok]
    if added:
        text = OUT_FILE.read_text(encoding="utf-8") if OUT_FILE.exists() else HEADER
        if not text.endswith("\n"):
            text += "\n"
        text += "".join(discover.line_for(a, s) + "\n" for a, s in added)
        OUT_FILE.write_text(text, encoding="utf-8")
    print(f"Added {len(added)} new companies: " + ", ".join(f"{a} {s}" for a, s in added))


if __name__ == "__main__":
    main()

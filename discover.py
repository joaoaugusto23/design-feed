#!/usr/bin/env python3
"""
Company finder: discovers companies that post jobs on the hiring systems the watcher reads,
keeps the ones that currently have US or remote openings, and saves them to
discovered.txt so the watcher checks them too.

Where the company names come from: Common Crawl (commoncrawl.org), a free public
archive of the web. Its index lists every job page it has saved on those three
sites, and each address contains the company's job-board name.

Runs once a week on GitHub (see .github/workflows/discover.yml). You don't need to edit it.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import watcher  # reuses the watcher's job-site readers and location rules

ROOT = watcher.ROOT
OUT_FILE = ROOT / "discovered.txt"

PATTERNS = [  # most useful first, in case the archive is slow and time runs out
    "job-boards.greenhouse.io/*", "boards.greenhouse.io/*", "jobs.lever.co/*", "jobs.ashbyhq.com/*",
    "*.myworkdayjobs.com", "jobs.smartrecruiters.com/*", "apply.workable.com/*",
    "*.breezy.hr", "*.bamboohr.com", "*.recruitee.com",
]
CRAWLS_TO_USE = 1                # the most recent archive
MAX_PAGES_PER_PATTERN = 300      # safety limit per site
TIME_PER_PATTERN = 10 * 60       # seconds per site, so one slow site can't use up the whole run
HARVEST_TIME_LIMIT = 100 * 60    # seconds spent reading the archive, at most
LOWERCASE_NAMES = {"greenhouse", "lever", "recruitee", "bamboohr", "breezy"}


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "personal-job-watcher/1.0 (company finder)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def get_retry(url, tries=4):
    wait = 5
    for i in range(tries):
        try:
            return get(url)
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"  retrying after error: {e}")
            time.sleep(wait)
            wait *= 3


def harvest():
    """Collect (ats, slug) pairs from the Common Crawl index."""
    started = time.time()
    crawls = json.loads(get_retry("https://index.commoncrawl.org/collinfo.json"))[:CRAWLS_TO_USE]
    found = set()
    for crawl in crawls:
        api = crawl["cdx-api"]
        for pattern in PATTERNS:
            q = f"{api}?url={urllib.parse.quote(pattern, safe='*/')}&output=json"
            try:
                pages = int(json.loads(get_retry(q + "&showNumPages=true")).get("pages", 0))
            except Exception as e:
                print(f"{crawl['id']} {pattern}: couldn't get page count ({e})")
                continue
            pages = min(pages, MAX_PAGES_PER_PATTERN)
            print(f"{crawl['id']} {pattern}: reading {pages} pages")
            before = len(found)
            pattern_started = time.time()
            for page in range(pages):
                if time.time() - started > HARVEST_TIME_LIMIT:
                    print("Time limit reached; using what was collected so far.")
                    return found
                if time.time() - pattern_started > TIME_PER_PATTERN:
                    print(f"  moving on after {page} pages (time limit for this site)")
                    break
                try:
                    text = get_retry(f"{q}&fl=url&page={page}")
                except Exception as e:
                    print(f"  page {page} skipped ({e})")
                    continue
                for line in text.splitlines():
                    try:
                        url = json.loads(line).get("url", "")
                    except ValueError:
                        continue
                    c = watcher.parse_company(url)
                    if c and len(c[1]) > 1:
                        found.add((c[0], c[1]))
            print(f"  +{len(found) - before} boards (total {len(found)})")
    return found


def worth_watching(company):
    """Keep a company if its board is live and has at least one US or remote job."""
    ats, slug = company
    try:
        jobs = watcher.FETCHERS[ats](slug, discovery=True)
    except watcher.RateLimited:
        time.sleep(10)
        try:
            jobs = watcher.FETCHERS[ats](slug, discovery=True)
        except Exception:
            return company, None
    except Exception:
        return company, False
    if any(watcher.looks_us_or_remote(j) for j in jobs):
        return company, True
    # Workday lists often say just "3 Locations"; give large employers the benefit of the doubt.
    return company, ats == "workday" and any(re.match(r"\d+ Locations", j["location"] or "") for j in jobs)


def line_for(ats, slug):
    if ats == "ashby" and not slug.replace("-", "").replace("_", "").replace(".", "").isalnum():
        return f"https://jobs.ashbyhq.com/{urllib.parse.quote(slug)}"
    return f"{ats} {slug}"


def main():
    manual_list, found_list = [], []
    watcher.read_company_file(ROOT / "companies.txt", "manual", set(), manual_list, [])
    watcher.read_company_file(OUT_FILE, "discovered", set(), found_list, [])
    manual = {(a, s.lower()) for a, s, _ in manual_list}
    existing = {(a, s) for a, s, _ in found_list}
    existing_keys = {(a, s.lower()) for a, s in existing}

    candidates = harvest()
    # Same board found under different capitalisation: keep one.
    # Most systems use lowercase names; Ashby, SmartRecruiters and Workday sites keep their capitals.
    by_key = {}
    for a, s in sorted(candidates, key=lambda c: (c[0], c[1].lower(), c[1] != c[1].lower())):
        if a in LOWERCASE_NAMES:
            s = s.lower()
        elif a == "workday":
            host, site = s.split("/", 1)
            s = f"{host.lower()}/{site}"
        by_key.setdefault((a, s.lower()), (a, s))
    to_test = [v for k, v in by_key.items() if k not in manual and k not in existing_keys]
    print(f"Found {len(by_key)} job boards; testing {len(to_test)} new ones.")

    keep = set(existing)
    with ThreadPoolExecutor(max_workers=24) as pool:
        for (ats, slug), ok in pool.map(worth_watching, to_test):
            if ok:
                keep.add((ats, slug))
    added = len(keep) - len(existing)
    print(f"Added {added} companies with US or remote openings. Total found automatically: {len(keep)}.")

    header = ("# Companies found automatically by discover.py (runs weekly).\n"
              "# Don't edit by hand; put your own picks in companies.txt instead.\n"
              "# Companies whose job pages disappear are skipped by the watcher automatically.\n")
    OUT_FILE.write_text(header + "\n".join(line_for(a, s) for a, s in sorted(keep, key=lambda x: (x[0], x[1].lower())))
                        + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

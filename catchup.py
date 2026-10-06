#!/usr/bin/env python3
"""
One-time catch-up: emails matching roles posted 4 to 7 days ago at the companies the
finder added automatically. Those roles were skipped when the watcher used a 3-day rule.
Run it by hand from the Actions tab ("One-time catch-up"). It doesn't change anything else.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape

import watcher as w

MAX_LISTED = 100


def main():
    companies, _ = w.load_companies()
    found = [c for c in companies if c[2] == "discovered"]
    newest, oldest = w.NOW - timedelta(days=3), w.NOW - timedelta(days=7)
    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(w.check_company, found))
    roles = []
    for _, jobs, err in results:
        if err:
            continue
        for job in jobs:
            ok, note = w.matches(job)
            if ok and job["posted"] and oldest <= job["posted"] < newest:
                job["note"] = note
                roles.append(job)
    roles.sort(key=lambda j: j["posted"], reverse=True)
    print(f"Checked {len(found)} companies. Roles posted 4 to 7 days ago: {len(roles)}.")
    if not roles:
        print("Nothing to send.")
        return
    shown = roles[:MAX_LISTED]
    extra = f" Showing the {MAX_LISTED} most recent." if len(roles) > MAX_LISTED else ""
    intro = (f"{len(roles)} matching roles were posted 4 to 7 days ago at companies added automatically."
             f"{extra} They're a few days old, so apply soon if one fits.")
    h, t = w.section("Catch-up: roles posted 4 to 7 days ago", intro, shown)
    html = (f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;'
            f'color:#1b2030;line-height:1.45">{h}</div>')
    w.send_email(f"Catch-up: {len(roles)} design roles posted 4 to 7 days ago", t, html)


if __name__ == "__main__":
    main()

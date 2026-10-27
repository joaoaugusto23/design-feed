#!/usr/bin/env python3
"""
Analyze one job on demand: compares your resume with a single job posting and emails you the
same fit score, matches, gaps and resume tweaks that the watcher adds to its alerts.

Run it from GitHub: Actions tab > "Analyze a job" > Run workflow, then paste a job link
(or, for pages that block automated reading such as LinkedIn, paste the job description text).

Uses the same secrets as the watcher: GMAIL_ADDRESS, GMAIL_APP_PASSWORD, GEMINI_API_KEY, RESUME_TEXT.
Inputs arrive as environment variables: JOB_URL, JOB_TEXT, JOB_TITLE, JOB_COMPANY.
"""
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from html import escape, unescape

import match_analysis as ma

MIN_TEXT = 150


def clean_text(s):
    return re.sub(r"\n\s*\n+", "\n", re.sub(r"[ \t]+", " ", s or "")).strip()


def from_ats_api(url):
    """Greenhouse, Lever and Ashby links: ask their public API, which returns clean text and the title."""
    q = urllib.parse.quote
    try:
        m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", url, re.I) \
            or re.search(r"greenhouse\.io/([\w-]+)\?.*?gh_jid=(\d+)", url, re.I)
        if m:
            d = ma._get(f"https://boards-api.greenhouse.io/v1/boards/{q(m.group(1))}/jobs/{q(m.group(2))}", True)
            return d.get("title"), d.get("company_name"), ma.html_to_text(unescape(d.get("content") or ""))
        m = re.search(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{16,})", url, re.I)
        if m:
            d = ma._get(f"https://api.lever.co/v0/postings/{q(m.group(1))}/{q(m.group(2))}", True)
            parts = [d.get("descriptionPlain") or ma.html_to_text(d.get("description"))]
            for item in d.get("lists") or []:
                parts.append((item.get("text") or "") + "\n" + ma.html_to_text(item.get("content")))
            parts.append(d.get("additionalPlain") or ma.html_to_text(d.get("additional")))
            return d.get("text"), m.group(1).replace("-", " ").title(), "\n".join(p for p in parts if p)
        m = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-f-]{16,})", url, re.I)
        if m:
            d = ma._get(f"https://api.ashbyhq.com/posting-api/job-board/{q(m.group(1))}", True)
            for j in d.get("jobs", []):
                if str(j.get("id")) == m.group(2):
                    return (j.get("title"), m.group(1).replace("-", " ").title(),
                            j.get("descriptionPlain") or ma.html_to_text(j.get("descriptionHtml")))
    except Exception as e:
        print(f"Couldn't use the hiring system's API ({e}), reading the page instead.")
    return None, None, ""


def from_page(url):
    """Any other link: look for the structured job data most career pages embed, else use the page text."""
    html = ma._get(url)
    title = company = text = ""
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            data = json.loads(unescape(block).strip())
        except ValueError:
            continue
        for item in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
            if isinstance(item, dict) and "JobPosting" in str(item.get("@type")):
                title = item.get("title") or title
                org = item.get("hiringOrganization")
                company = (org.get("name") if isinstance(org, dict) else org) or company
                text = ma.html_to_text(unescape(item.get("description") or "")) or text
    if len(text) < MIN_TEXT:
        text = ma.html_to_text(html)
    if not title:
        m = re.search(r'(?is)<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']', html) \
            or re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        title = clean_text(unescape(m.group(1))) if m else ""
    return title, company, text


def load_posting(url):
    title, company, text = from_ats_api(url)
    if len(text or "") < MIN_TEXT:
        title2, company2, text = from_page(url)
        title, company = title or title2, company or company2
    return title, company, text


def main():
    url = os.environ.get("JOB_URL", "").strip()
    pasted = os.environ.get("JOB_TEXT", "").strip()
    title = os.environ.get("JOB_TITLE", "").strip()
    company = os.environ.get("JOB_COMPANY", "").strip()
    if not url and not pasted:
        sys.exit("Give either a job link or the job description text.")

    text, problem = pasted, None
    if len(text) < MIN_TEXT and url:
        try:
            t, c, text = load_posting(url)
            title, company = title or t or "", company or c or ""
        except Exception as e:
            text, problem = "", (f"couldn't open the link ({e}). Many sites block automated reading. "
                                 "Run it again and paste the job description into the 'job_text' box.")
    text = clean_text(text)[:ma.MAX_JD_CHARS]
    if len(text) < MIN_TEXT and not problem:
        problem = ("the page didn't contain a readable job description. Many sites, LinkedIn especially, block "
                   "automated reading. Run it again and paste the job description into the 'job_text' box.")

    job = {"id": "", "title": title or "this role", "company": company or "Company not found",
           "location": "", "url": url or "", "posted": None, "pay": None, "note": None}
    if problem:
        job["analysis_note"] = problem
    else:
        ma.analyze_jobs([job], describe=lambda _j: text)

    import watcher  # reuses the alert email's look and the Gmail sending code
    body = watcher.job_html(job)
    if not url:   # pasted text has no link, so drop the "Open and apply" button
        body = re.sub(r'<div style="margin-top:8px"><a href="".*?Open and apply</a></div>', "", body, flags=re.S)
    status = ("Fit " + str(job["analysis"]["score"]) + "/100") if job.get("analysis") else "no score"
    subject = f"Resume match: {job['title']} at {job['company']} ({status})"
    html = ('<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;'
            'color:#1b2030;line-height:1.45"><p style="font-size:15px">Here is how your resume compares with this posting.</p>'
            f'<table style="width:100%;border-collapse:collapse">{body}</table></div>')
    plain = watcher.job_text(job)
    watcher.send_email(subject, plain, html)
    print(f"Done: {subject} ({datetime.now(timezone.utc).isoformat(timespec='seconds')})")


if __name__ == "__main__":
    main()

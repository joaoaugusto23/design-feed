#!/usr/bin/env python3
"""
Job watcher: checks company job pages on Greenhouse, Lever and Ashby,
finds new design roles that match settings.toml, and emails you about them.

You shouldn't need to edit this file. Change settings.toml and companies.txt instead.
"""
import json
import os
import re
import smtplib
import ssl
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEEN_FILE = ROOT / "seen.json"
STATUS_FILE = ROOT / "status.md"
NOW = datetime.now(timezone.utc)
DRY_RUN = os.environ.get("DRY_RUN") == "1"

SETTINGS = tomllib.loads((ROOT / "settings.toml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- companies list

URL_PATTERNS = [
    ("greenhouse", re.compile(r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/([\w.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([^/?#\s]+)", re.I)),
]


def parse_company(line):
    """Accepts 'greenhouse figma' or any job URL like https://jobs.lever.co/acme/123."""
    for ats, pattern in URL_PATTERNS:
        m = pattern.search(line)
        if m:
            return (ats, urllib.parse.unquote(m.group(1)))
    parts = line.replace(":", " ").split()
    if len(parts) == 2 and parts[0].lower() in ("greenhouse", "lever", "ashby"):
        return (parts[0].lower(), parts[1])
    return None


def load_companies():
    companies, problems, seen = [], [], set()
    for n, raw in enumerate((ROOT / "companies.txt").read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        c = parse_company(line)
        if not c:
            problems.append(f"Line {n} of companies.txt wasn't understood: `{line}`")
            continue
        key = (c[0], c[1].lower())
        if key not in seen:
            seen.add(key)
            companies.append(c)
    return companies, problems


# ---------------------------------------------------------------- fetching

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "personal-job-watcher/1.0", "Accept": "application/json"})
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise RuntimeError("not found (the company name in companies.txt may be wrong)")
            last = e
        except Exception as e:  # network hiccup, timeout, bad JSON
            last = e
    raise RuntimeError(f"couldn't load ({last})")


def parse_time(value):
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def pretty(slug):
    return re.sub(r"[-_]+", " ", slug).strip().title()


def get_greenhouse(slug):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(slug)}/jobs")
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "id": f"greenhouse:{slug.lower()}:{j.get('id')}",
            "company": j.get("company_name") or pretty(slug),
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", "") or "",
            "url": j.get("absolute_url", ""),
            "posted": parse_time(j.get("first_published")),
            "workplace": None, "remote_flag": None, "country": None, "pay": None,
        })
    return jobs


def get_lever(slug):
    data = fetch_json(f"https://api.lever.co/v0/postings/{urllib.parse.quote(slug)}?mode=json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        locs = [cats.get("location") or ""] + list(cats.get("allLocations") or [])
        pay = None
        sr = j.get("salaryRange") or {}
        if sr.get("min") and sr.get("max"):
            pay = f"{sr.get('currency', '')} {sr['min']:,}–{sr['max']:,}".strip()
        jobs.append({
            "id": f"lever:{slug.lower()}:{j.get('id')}",
            "company": pretty(slug),
            "title": j.get("text", ""),
            "location": "; ".join(dict.fromkeys(l for l in locs if l)),
            "url": j.get("hostedUrl", ""),
            "posted": parse_time(j.get("createdAt")),
            "workplace": (j.get("workplaceType") or "").lower() or None,
            "remote_flag": None,
            "country": j.get("country"),
            "pay": pay,
        })
    return jobs


def get_ashby(slug):
    data = fetch_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(slug)}?includeCompensation=true")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [(s or {}).get("location", "") for s in j.get("secondaryLocations") or []]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        jobs.append({
            "id": f"ashby:{slug.lower()}:{j.get('id')}",
            "company": pretty(slug),
            "title": j.get("title", ""),
            "location": "; ".join(dict.fromkeys(l for l in locs if l)),
            "url": j.get("jobUrl") or j.get("applyUrl") or "",
            "posted": parse_time(j.get("publishedAt")),
            "workplace": (j.get("workplaceType") or "").lower().replace("onsite", "on-site") or None,
            "remote_flag": j.get("isRemote"),
            "country": addr.get("addressCountry"),
            "pay": ((j.get("compensation") or {}).get("compensationTierSummary")),
        })
    return jobs


FETCHERS = {"greenhouse": get_greenhouse, "lever": get_lever, "ashby": get_ashby}


def check_company(company):
    ats, slug = company
    try:
        return company, FETCHERS[ats](slug), None
    except Exception as e:
        return company, [], str(e)


# ---------------------------------------------------------------- matching

US_STATES = ("alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|"
             "idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
             "minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|"
             "new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|"
             "south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|"
             "wisconsin|wyoming")
US_CODES = ("AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|"
            "NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC")
US_RE = re.compile(r"(?<![\w.])(united states|usa|u\.s\.a?\.?|us|north america|americas|nationwide|" + US_STATES + r")(?![\w])", re.I)
US_CODE_RE = re.compile(r",\s*(" + US_CODES + r")\b")
NON_US_RE = re.compile(
    r"\b(canada|toronto|vancouver|montreal|mexico|brazil|brasil|são paulo|sao paulo|latam|latin america|argentina|"
    r"colombia|chile|uk|united kingdom|england|london|ireland|dublin|europe|emea|germany|berlin|munich|france|paris|"
    r"spain|madrid|barcelona|netherlands|amsterdam|poland|warsaw|portugal|lisbon|sweden|stockholm|switzerland|"
    r"zurich|italy|india|bangalore|bengaluru|hyderabad|apac|asia|australia|sydney|melbourne|new zealand|singapore|"
    r"japan|tokyo|korea|seoul|israel|tel aviv|philippines|manila|vietnam|indonesia|china|hong kong|taiwan|"
    r"south africa|nigeria|kenya|egypt|uae|dubai)\b", re.I)
REMOTE_RE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)


def word_in(term, text):
    return re.search(r"(?<![\w])" + re.escape(term.lower()) + r"(?![\w])", text) is not None


def matches(job):
    """Returns (is_match, note_for_email)."""
    title = job["title"].lower()
    if not any(t.lower() in title for t in SETTINGS.get("include_titles", [])):
        return False, None
    if any(word_in(t, title) for t in SETTINGS.get("exclude_titles", [])):
        return False, None

    loc = job["location"] or ""
    wp = job["workplace"]
    if SETTINGS.get("require_remote", True):
        if wp in ("hybrid", "on-site", "onsite"):
            return False, None
        is_remote = wp == "remote" or job["remote_flag"] is True or bool(REMOTE_RE.search(loc))
        if not is_remote:
            return False, None

    if SETTINGS.get("require_us", True):
        country = (job["country"] or "").strip().lower()
        is_us = country in ("us", "usa", "united states", "united states of america") \
            or bool(US_RE.search(loc)) or bool(US_CODE_RE.search(loc))
        if not is_us:
            if country or NON_US_RE.search(loc):
                return False, None
            if not SETTINGS.get("include_unclear_location", True):
                return False, None
            return True, "Location doesn't say which country. Check it's open to the US before applying."
    return True, None


# ---------------------------------------------------------------- state

def load_state():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {"companies": [], "jobs": {}}


def save_state(state, failed, problems, total_companies):
    SEEN_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Job watcher status", "",
             f"Watching {total_companies - len(failed)} of {total_companies} companies successfully.", ""]
    if failed or problems:
        lines += ["## Needs attention", "",
                  "These lines in companies.txt couldn't be checked. Fix the name or delete the line.", ""]
        lines += [f"- {ats} `{slug}`: {err}" for (ats, slug), err in failed]
        lines += [f"- {p}" for p in problems]
    else:
        lines.append("Everything on your list is working.")
    STATUS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- email

def age(dt):
    if not dt:
        return ""
    mins = int((NOW - dt).total_seconds() // 60)
    if mins < 1:
        return "posted just now"
    if mins < 60:
        return f"posted {mins} min ago"
    if mins < 60 * 48:
        return f"posted {mins // 60} h ago"
    return f"posted {mins // 1440} days ago"


def job_html(j):
    bits = [escape(j["company"])]
    if j["location"]:
        bits.append(escape(j["location"]))
    if j["posted"]:
        bits.append(age(j["posted"]))
    extra = ""
    if j.get("pay"):
        extra += f'<div style="color:#3b4255;font-size:14px;margin-top:2px">Pay: {escape(str(j["pay"]))}</div>'
    if j.get("note"):
        extra += f'<div style="color:#8a5a00;font-size:14px;margin-top:2px">{escape(j["note"])}</div>'
    return (f'<tr><td style="padding:14px 0;border-bottom:1px solid #e3e6ee">'
            f'<a href="{escape(j["url"])}" style="font-size:17px;font-weight:600;color:#2f4bd8;text-decoration:none">'
            f'{escape(j["title"])}</a>'
            f'<div style="color:#5d6478;font-size:14px;margin-top:2px">{" &nbsp;|&nbsp; ".join(bits)}</div>{extra}'
            f'<div style="margin-top:8px"><a href="{escape(j["url"])}" style="display:inline-block;background:#2f4bd8;'
            f'color:#fff;padding:7px 14px;border-radius:8px;text-decoration:none;font-size:14px">Open and apply</a>'
            f'</div></td></tr>')


def job_text(j):
    parts = [f"{j['title']} at {j['company']}", f"  {j['location']}" if j["location"] else None,
             f"  {age(j['posted'])}" if j["posted"] else None,
             f"  Pay: {j['pay']}" if j.get("pay") else None,
             f"  Note: {j['note']}" if j.get("note") else None, f"  {j['url']}"]
    return "\n".join(p for p in parts if p)


def section(title, intro, jobs):
    if not jobs:
        return "", ""
    h = (f'<h2 style="font-size:18px;margin:26px 0 4px">{escape(title)}</h2>'
         f'<p style="color:#5d6478;margin:0 0 6px;font-size:14px">{escape(intro)}</p>'
         f'<table style="width:100%;border-collapse:collapse">{"".join(job_html(j) for j in jobs)}</table>')
    t = f"\n{title.upper()}\n{intro}\n\n" + "\n\n".join(job_text(j) for j in jobs) + "\n"
    return h, t


def build_email(new, already, failed, problems, first_run, total):
    if first_run:
        subject = f"Job watcher is set up: {len(already)} matching role{'s' if len(already) != 1 else ''} open right now"
        lead = (f"Your job watcher is working. It checked {total - len(failed)} companies. "
                f"From now on you'll get an email only when a new matching role appears.")
    else:
        names = list(dict.fromkeys(j["company"] for j in new or already))
        shown = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
        n = len(new)
        subject = (f"{n} new design role{'s' if n != 1 else ''}: {shown}" if new
                   else f"Roles already open at companies you added: {shown}")
        lead = "Apply soon while the list of applicants is still short." if new else ""

    h1, t1 = section("New since the last check", "These just appeared.", new)
    h2, t2 = section("Already open" if first_run else "Already open at companies you just added",
                     "These were open before the watcher started checking, so they aren't brand new.", already)
    fix_h = fix_t = ""
    if failed or problems:
        items = [f"{ats} {slug}: {err}" for (ats, slug), err in failed] + problems
        fix_h = ('<h2 style="font-size:16px;margin:26px 0 4px">Couldn’t check</h2>'
                 '<p style="color:#5d6478;margin:0 0 6px;font-size:14px">Fix or remove these lines in companies.txt. '
                 'The rest of your list is still being watched.</p><ul style="color:#5d6478;font-size:14px">'
                 + "".join(f"<li>{escape(i)}</li>" for i in items) + "</ul>")
        fix_t = "\nCOULDN'T CHECK (fix or remove in companies.txt)\n" + "\n".join(f"- {i}" for i in items) + "\n"
    if first_run and not already:
        h2 = ('<p style="font-size:15px">No matching roles are open right now at the companies on your list. '
              'You’ll hear from the watcher as soon as one appears.</p>')
        t2 = "\nNo matching roles are open right now. You'll get an email as soon as one appears.\n"

    html = (f'<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:640px;'
            f'color:#1b2030;line-height:1.45"><p style="font-size:15px">{escape(lead)}</p>'
            f'{h1}{h2}{fix_h}</div>')
    text = f"{lead}\n{t1}{t2}{fix_t}"
    return subject, text, html


def send_email(subject, text, html):
    sender = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    to = os.environ.get("ALERT_TO", "").strip() or sender
    if DRY_RUN:
        print(f"[dry run] Would email {to or '(no address)'}:\nSubject: {subject}\n\n{text}")
        return
    if not sender or not password:
        sys.exit("Missing GMAIL_ADDRESS or GMAIL_APP_PASSWORD. Add them under Settings > Secrets and variables > Actions.")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Job watcher <{sender}>", to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(sender, password)
        s.send_message(msg)
    print(f"Emailed {to}: {subject}")


# ---------------------------------------------------------------- main

def main():
    companies, problems = load_companies()
    if not companies:
        sys.exit("companies.txt has no companies in it yet.")
    first_run = not SEEN_FILE.exists()
    state = load_state()
    known_companies = set(state.get("companies", []))
    seen = state.setdefault("jobs", {})

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(check_company, companies))

    new, already, failed = [], [], []
    for (ats, slug), jobs, err in results:
        key = f"{ats}:{slug.lower()}"
        if err:
            failed.append(((ats, slug), err))
            print(f"Couldn't check {ats} {slug}: {err}")
            continue
        newly_added = key not in known_companies
        for job in jobs:
            ok, note = matches(job)
            if not ok:
                continue
            job["note"] = note
            if job["id"] in seen:
                seen[job["id"]] = NOW.isoformat()  # still open; refresh so it isn't pruned
                continue
            seen[job["id"]] = NOW.isoformat()
            (already if (first_run or newly_added) else new).append(job)
        known_companies.add(key)

    # Forget jobs not seen open for 120 days so the file stays small.
    cutoff = NOW - timedelta(days=120)
    state["jobs"] = {k: v for k, v in seen.items() if parse_time(v) and parse_time(v) > cutoff}
    state["companies"] = sorted(known_companies)

    sort_key = lambda j: j["posted"] or datetime.min.replace(tzinfo=timezone.utc)
    new.sort(key=sort_key, reverse=True)
    already.sort(key=sort_key, reverse=True)
    print(f"Checked {len(companies) - len(failed)}/{len(companies)} companies. "
          f"New: {len(new)}. Already open: {len(already)}.")

    if first_run or new or already:
        send_email(*build_email(new, already, failed, problems, first_run, len(companies)))
    if not DRY_RUN:
        save_state(state, failed, problems, len(companies))


if __name__ == "__main__":
    main()

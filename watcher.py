#!/usr/bin/env python3
"""
Job watcher: checks company job pages on nine hiring systems,
finds new design roles that match settings.toml, and emails you about them.

Reads Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee, BambooHR, Breezy and Workday.
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
    ("workable", re.compile(r"apply\.workable\.com/(?!j/|api/)([\w-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)),
    ("recruitee", re.compile(r"(?:https?://)?([\w-]+)\.recruitee\.com", re.I)),
    ("bamboohr", re.compile(r"(?:https?://)?([\w-]+)\.bamboohr\.com", re.I)),
    ("breezy", re.compile(r"(?:https?://)?([\w-]+)\.breezy\.hr", re.I)),
    ("workday", re.compile(r"(?:https?://)?([\w-]+\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)", re.I)),
]
ATS_NAMES = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "bamboohr", "breezy", "workday")
NOT_COMPANY = {"www", "api", "app", "help", "support", "blog", "jobs", "j", "careers", "embed", "marketplace",
               "partners", "status", "docs", "wday", "job", "oneclick-ui", "sr-jobs", "static", "assets", "login"}


def parse_company(line):
    """Accepts 'greenhouse figma' or any job URL like https://jobs.lever.co/acme/123."""
    for ats, pattern in URL_PATTERNS:
        m = pattern.search(line)
        if m:
            if ats == "workday":
                slug = f"{m.group(1).lower()}/{m.group(2)}"
                if m.group(2).lower() in NOT_COMPANY:
                    return None
                return (ats, slug)
            slug = urllib.parse.unquote(m.group(1))
            if slug.lower() in NOT_COMPANY:
                return None
            return (ats, slug)
    parts = line.replace(":", " ").split()
    if len(parts) == 2 and parts[0].lower() in ATS_NAMES:
        return (parts[0].lower(), parts[1])
    return None


def read_company_file(path, source, seen, companies, problems):
    if not path.exists():
        return
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        c = parse_company(line)
        if not c:
            if source == "manual":
                problems.append(f"Line {n} of companies.txt wasn't understood: '{line}'")
            continue
        key = (c[0], c[1].lower())
        if key not in seen:
            seen.add(key)
            companies.append((c[0], c[1], source))


def load_companies():
    """companies.txt is your hand-picked list; discovered.txt is filled in by discover.py."""
    companies, problems, seen = [], [], set()
    read_company_file(ROOT / "companies.txt", "manual", seen, companies, problems)
    read_company_file(ROOT / "discovered.txt", "discovered", seen, companies, problems)
    return companies, problems


# ---------------------------------------------------------------- fetching

class NotFound(Exception):
    pass


class RateLimited(Exception):
    pass


def fetch_json(url, body=None):
    headers = {"User-Agent": "personal-job-watcher/1.0", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    last = None
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound("not found (the company name in companies.txt may be wrong)")
            if e.code == 429:
                raise RateLimited("the job site asked us to slow down; will retry next run")
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
        text = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
        if len(text) > 10 and text[10] == " ":
            text = text[:10] + "T" + text[11:]
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def pretty(slug):
    return re.sub(r"[-_]+", " ", slug).strip().title()


def get_greenhouse(slug, discovery=False):
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


def get_lever(slug, discovery=False):
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


def get_ashby(slug, discovery=False):
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


def job(id_, company, title, location, url, posted=None, workplace=None, remote_flag=None, country=None, pay=None):
    return {"id": id_, "company": company, "title": title or "", "location": location or "", "url": url or "",
            "posted": posted, "workplace": workplace, "remote_flag": remote_flag, "country": country, "pay": pay}


def join_loc(*parts):
    return ", ".join(str(p) for p in parts if p)


def get_workable(slug, discovery=False):
    data = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{urllib.parse.quote(slug)}")
    company = data.get("name") or pretty(slug)
    jobs = []
    for j in data.get("jobs", []):
        locs = [l for l in (j.get("locations") or []) if not l.get("hidden")] or \
               [{"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}]
        text = "; ".join(join_loc(l.get("city"), l.get("region"), l.get("country")) for l in locs)
        remote = j.get("telecommuting") is True
        if remote:
            text = (text + "; Remote").strip("; ")
        country = (locs[0].get("countryCode") or locs[0].get("country")) if len(locs) == 1 else None
        jobs.append(job(f"workable:{slug.lower()}:{j.get('shortcode')}", company, j.get("title"), text,
                        j.get("url") or j.get("application_url"), parse_time(j.get("published_on")),
                        "remote" if remote else None, remote or None, country))
    return jobs


def get_smartrecruiters(slug, discovery=False):
    jobs, offset = [], 0
    query = "" if discovery else "&q=designer"
    while True:
        data = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(slug)}/postings"
                          f"?limit=100&offset={offset}{query}")
        content = data.get("content") or []
        for j in content:
            loc = j.get("location") or {}
            wp = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else "on-site"
            text = loc.get("fullLocation") or join_loc(loc.get("city"), loc.get("region"), loc.get("country"))
            if loc.get("remote"):
                text += "; Remote"
            jobs.append(job(f"smartrecruiters:{slug.lower()}:{j.get('id')}",
                            (j.get("company") or {}).get("name") or pretty(slug), j.get("name"), text,
                            f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}", parse_time(j.get("releasedDate")),
                            wp, bool(loc.get("remote")), loc.get("country")))
        offset += len(content)
        if discovery or not content or offset >= int(data.get("totalFound") or 0) or offset >= 300:
            return jobs


def get_recruitee(slug, discovery=False):
    data = fetch_json(f"https://{urllib.parse.quote(slug)}.recruitee.com/api/offers/")
    jobs = []
    for j in data.get("offers", []):
        if j.get("status") not in (None, "published"):
            continue
        remote = j.get("remote") is True
        wp = "remote" if remote else "hybrid" if j.get("hybrid") else None
        text = j.get("location") or join_loc(j.get("city"), j.get("country"))
        if remote and "remote" not in text.lower():
            text += "; Remote"
        jobs.append(job(f"recruitee:{slug.lower()}:{j.get('id')}", j.get("company_name") or pretty(slug), j.get("title"),
                        text, j.get("careers_url") or j.get("careers_apply_url"), parse_time(j.get("published_at")),
                        wp, remote or None, j.get("country_code")))
    return jobs


def get_bamboohr(slug, discovery=False):
    data = fetch_json(f"https://{urllib.parse.quote(slug)}.bamboohr.com/careers/list")
    jobs = []
    for j in data.get("result", []):
        loc = j.get("location") or {}
        ats = j.get("atsLocation") or {}
        remote = j.get("isRemote") is True
        text = join_loc(loc.get("city") or ats.get("city"), loc.get("state") or ats.get("state"),
                        ats.get("country") or loc.get("country"))
        if remote:
            text = (text + "; Remote").strip("; ")
        jobs.append(job(f"bamboohr:{slug.lower()}:{j.get('id')}", pretty(slug), j.get("jobOpeningName"), text,
                        f"https://{slug}.bamboohr.com/careers/{j.get('id')}", None,
                        "remote" if remote else None, remote or None, ats.get("country") or loc.get("country")))
    return jobs


def get_breezy(slug, discovery=False):
    data = fetch_json(f"https://{urllib.parse.quote(slug)}.breezy.hr/json")
    jobs = []
    for j in data if isinstance(data, list) else []:
        loc = j.get("location") or {}
        country = (loc.get("country") or {}).get("name") if isinstance(loc.get("country"), dict) else loc.get("country")
        remote = loc.get("is_remote") is True
        text = loc.get("name") or join_loc(loc.get("city"), country)
        if remote and "remote" not in text.lower():
            text += "; Remote"
        jobs.append(job(f"breezy:{slug.lower()}:{j.get('id')}", (j.get("company") or {}).get("name") or pretty(slug),
                        j.get("name"), text, j.get("url"), parse_time(j.get("published_date")),
                        "remote" if remote else None, remote or None, country))
    return jobs


SEEN_IDS = set()   # filled in by main() so Workday details aren't fetched again for known jobs


def get_workday(slug, discovery=False):
    host, site = slug.split("/", 1)
    tenant = host.split(".")[0]
    base = f"https://{host}.myworkdayjobs.com"
    api = f"{base}/wday/cxs/{tenant}/{site}"
    searches = [""] if discovery else ["product designer", "ux designer", "ui designer"]
    jobs, seen_paths = [], set()
    for text in searches:
        data = fetch_json(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": text})
        for p in data.get("jobPostings") or []:
            path = p.get("externalPath") or ""
            if not path or path in seen_paths:
                continue
            seen_paths.add(path)
            jid = f"workday:{slug.lower()}:{path.rsplit('_', 1)[-1]}"
            title = p.get("title") or ""
            loc_text = p.get("locationsText") or ""
            entry = job(jid, pretty(tenant), title, loc_text, f"{base}/{site}{path}")
            # Only look up full details for new jobs whose title is a match.
            if not discovery and jid not in SEEN_IDS and title_ok(title):
                try:
                    info = fetch_json(f"{api}{path}").get("jobPostingInfo") or {}
                    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
                    entry["location"] = "; ".join(l for l in locs if l) or loc_text
                    entry["country"] = (info.get("country") or {}).get("descriptor")
                    entry["posted"] = parse_time(info.get("startDate"))
                    entry["url"] = info.get("externalUrl") or entry["url"]
                    if "remote" in entry["location"].lower():
                        entry["workplace"] = "remote"
                        entry["country"] = None  # remote roles may list several countries; judge by location text
                except Exception:
                    pass
            jobs.append(entry)
    return jobs


FETCHERS = {"greenhouse": get_greenhouse, "lever": get_lever, "ashby": get_ashby, "workable": get_workable,
            "smartrecruiters": get_smartrecruiters, "recruitee": get_recruitee, "bamboohr": get_bamboohr,
            "breezy": get_breezy, "workday": get_workday}


def check_company(company):
    ats, slug, source = company
    try:
        return company, FETCHERS[ats](slug), None
    except Exception as e:
        return company, [], e


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


def looks_us_or_remote(job):
    loc = job["location"] or ""
    country = (job["country"] or "").strip().lower()
    return (job["workplace"] == "remote" or job["remote_flag"] is True or bool(REMOTE_RE.search(loc))
            or country in ("us", "usa", "united states") or bool(US_RE.search(loc)) or bool(US_CODE_RE.search(loc)))


def title_ok(title):
    title = (title or "").lower()
    return (any(t.lower() in title for t in SETTINGS.get("include_titles", []))
            and not any(word_in(t, title) for t in SETTINGS.get("exclude_titles", [])))


def matches(job):
    """Returns (is_match, note_for_email)."""
    if not title_ok(job["title"]):
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


def save_state(state, failed, problems, counts):
    SEEN_FILE.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    lines = ["# Job watcher status", "",
             f"Your list (companies.txt): watching {counts['manual_ok']} of {counts['manual']} successfully.", ""]
    if counts["discovered"]:
        lines += [f"Found automatically (discovered.txt): watching {counts['discovered']} companies. "
                  f"{counts['dropped']} were dropped because their job pages no longer exist.", ""]
    if failed or problems:
        lines += ["## Needs attention", "",
                  "These lines in companies.txt couldn't be checked. Fix the name or delete the line.", ""]
        lines += [f"- {ats} '{slug}': {err}" for (ats, slug), err in failed]
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
                   else f"Recent design roles at newly added companies: {shown}")
        lead = "Apply soon while the list of applicants is still short." if new else ""

    h1, t1 = section("New since the last check", "These just appeared.", new)
    h2, t2 = section("Already open" if first_run else "At companies just added to your watch list",
                     "These were posted before the watcher started checking these companies, "
                     "so they aren't brand new, but they're recent enough to be worth a look.", already)
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

RECENT_DAYS = 7        # for newly found companies, still mention roles posted this recently
MAX_RECENT_LISTED = 60
DROP_AFTER_404S = 3    # auto-found companies are dropped after this many "not found" checks in a row


def main():
    companies, problems = load_companies()
    if not companies:
        sys.exit("companies.txt has no companies in it yet.")
    first_run = not SEEN_FILE.exists()
    state = load_state()
    known_companies = set(state.get("companies", []))
    seen = state.setdefault("jobs", {})
    fails = state.setdefault("fails", {})
    dropped = set(state.get("dropped", []))

    SEEN_IDS.update(seen)
    to_check = [c for c in companies if not (c[2] == "discovered" and f"{c[0]}:{c[1].lower()}" in dropped)]
    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(check_company, to_check))

    new, already, failed = [], [], []
    manual_total = sum(1 for c in companies if c[2] == "manual")
    recent_cutoff = NOW - timedelta(days=RECENT_DAYS)
    for (ats, slug, source), jobs, err in results:
        key = f"{ats}:{slug.lower()}"
        if err:
            if source == "manual":
                failed.append(((ats, slug), str(err)))
                print(f"Couldn't check {ats} {slug}: {err}")
            elif isinstance(err, NotFound):
                fails[key] = fails.get(key, 0) + 1
                if fails[key] >= DROP_AFTER_404S:
                    dropped.add(key)
                    fails.pop(key, None)
            continue
        fails.pop(key, None)
        newly_added = key not in known_companies
        quiet = newly_added and source == "discovered" and not first_run
        for job in jobs:
            if job["id"] in seen:
                seen[job["id"]] = NOW.isoformat()  # still open; refresh so it isn't pruned
                continue
            ok, note = matches(job)
            if not ok:
                continue
            job["note"] = note
            seen[job["id"]] = NOW.isoformat()
            if quiet:
                # A company the finder just added: only mention roles that are still fresh.
                if job["posted"] and job["posted"] >= recent_cutoff:
                    already.append(job)
            elif first_run or newly_added:
                if source == "manual" or (job["posted"] and job["posted"] >= recent_cutoff):
                    already.append(job)
            else:
                new.append(job)
        known_companies.add(key)

    cutoff = NOW - timedelta(days=120)
    state["jobs"] = {k: v for k, v in seen.items() if parse_time(v) and parse_time(v) > cutoff}
    state["companies"] = sorted(known_companies)
    state["dropped"] = sorted(dropped)
    state["fails"] = fails

    sort_key = lambda j: j["posted"] or datetime.min.replace(tzinfo=timezone.utc)
    new.sort(key=sort_key, reverse=True)
    already.sort(key=sort_key, reverse=True)
    already = already[:MAX_RECENT_LISTED] if len(already) > MAX_RECENT_LISTED else already
    counts = {"manual": manual_total, "manual_ok": manual_total - len(failed),
              "discovered": sum(1 for c in to_check if c[2] == "discovered"), "dropped": len(dropped)}
    print(f"Checked {len(to_check)} companies ({counts['discovered']} found automatically). "
          f"New: {len(new)}. Already open: {len(already)}.")

    if first_run or new or already:
        send_email(*build_email(new, already, failed, problems, first_run, len(to_check)))
    if not DRY_RUN:
        save_state(state, failed, problems, counts)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Resume match analysis: for each role the watcher is about to email, reads the job description,
compares it with your resume using Google's Gemini API (free tier), and adds a short
"how well do I match" block to the alert: a fit score, what matches, what's missing,
and wording tweaks to your existing resume.

It needs two secrets on GitHub (Settings > Secrets and variables > Actions):
  GEMINI_API_KEY   a free key from aistudio.google.com/apikey
  RESUME_TEXT      your resume as plain text (leave your phone and address out of it)
Without them this file does nothing and the alerts look exactly like before.

Safe by design: if anything fails (no key, rate limit, a page that won't load), the job
simply goes out in the email without the analysis. It never blocks an alert.

It only runs on roles that are about to be emailed, capped per run, so usage stays tiny.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html import escape, unescape

MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-flash"
MAX_JOBS_PER_RUN = 15          # most roles analyzed in one email
SECONDS_BETWEEN_CALLS = 5      # keeps us under the free tier's per-minute limit
TIME_BUDGET_SECONDS = 240      # stop analyzing after this long, send the email anyway
MAX_JD_CHARS = 12000
MAX_RESUME_CHARS = 15000


class RateLimited(Exception):
    pass


# ---------------------------------------------------------------- fetching the job description

def _get(url, as_json=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": "personal-job-watcher/1.0",
        "Accept": "application/json" if as_json else "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode("utf-8", errors="replace")
    return json.loads(raw) if as_json else raw


def html_to_text(html):
    if not html:
        return ""
    html = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>|</tr>", "\n", html)
    html = re.sub(r"(?i)<li[^>]*>", "- ", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def fetch_description(job):
    """Best effort. Returns plain text of the posting, or '' if it can't be loaded."""
    parts = (job.get("id") or "").split(":", 2)
    ats, slug, jid = (parts + ["", "", ""])[:3]
    text = ""
    try:
        q = urllib.parse.quote
        if ats == "greenhouse":
            data = _get(f"https://boards-api.greenhouse.io/v1/boards/{q(slug)}/jobs/{q(jid)}", True)
            text = html_to_text(unescape(data.get("content") or ""))
        elif ats == "lever":
            data = _get(f"https://api.lever.co/v0/postings/{q(slug)}/{q(jid)}", True)
            bits = [data.get("descriptionPlain") or html_to_text(data.get("description"))]
            for item in data.get("lists") or []:
                bits.append((item.get("text") or "") + "\n" + html_to_text(item.get("content")))
            bits.append(data.get("additionalPlain") or html_to_text(data.get("additional")))
            text = "\n".join(b for b in bits if b)
        elif ats == "ashby":
            data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{q(slug)}", True)
            for j in data.get("jobs", []):
                if str(j.get("id")) == jid:
                    text = j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))
                    break
    except Exception as e:
        print(f"  Couldn't load description via API for {job.get('title')}: {e}")
    if len(text) < 200 and job.get("url"):          # other job systems, or an API miss: read the page itself
        try:
            text = html_to_text(_get(job["url"])) or text
        except Exception as e:
            print(f"  Couldn't load page for {job.get('title')}: {e}")
    return text[:MAX_JD_CHARS]


# ---------------------------------------------------------------- asking Gemini

SYSTEM = (
    "You compare one resume with one job description and help the candidate decide how to apply. "
    "Use only facts that are in the resume. Never invent experience, tools, numbers or titles. "
    "Be honest and specific, not flattering. Do not predict the chance of an interview. "
    "Write short plain phrases. Do not use em dashes or en dashes."
)

PROMPT = """RESUME:
{resume}

JOB: {title} at {company}
{location_line}
JOB DESCRIPTION:
{jd}

Return JSON only, with exactly these keys:
{{
  "score": integer 0 to 100. How well the resume fits what this posting asks for. Weigh required skills,
           domain, seniority and years of experience. 85+ means nearly everything required is clearly shown,
           65 to 84 means a solid fit with a few gaps, 45 to 64 means a stretch, below 45 means a poor fit,
  "verdict": one of "Strong fit", "Good fit", "Stretch", "Weak fit",
  "matches": up to 4 short strings, the strongest overlaps between the resume and the posting,
  "gaps": up to 4 short strings, requirements or keywords the posting wants that the resume does not show,
  "keywords_to_add": up to 6 short strings, terms from the posting to work into the resume, ONLY where the
           resume already truthfully supports them,
  "tweaks": up to 3 short strings, concrete ways to reword or reorder existing resume content for this role,
  "lead_with": one short sentence on which experience to put first for this role
}}"""


def call_gemini(prompt):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{urllib.parse.quote(MODEL)}:generateContent"
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited("Gemini free limit reached")
        raise RuntimeError(f"Gemini error {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}")
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError(f"Gemini returned no answer ({data.get('promptFeedback')})")
    text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []))
    return parse_answer(text)


def parse_answer(text):
    """Accepts clean JSON, or JSON wrapped in a code fence or extra words."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError("Gemini's answer wasn't JSON")
        data = json.loads(m.group(0))
    return clean(data)


def _strs(value, limit):
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()][:limit]


def clean(data):
    try:
        score = max(0, min(100, int(round(float(data.get("score"))))))
    except (TypeError, ValueError):
        raise RuntimeError("Gemini's answer had no score")
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in ("Strong fit", "Good fit", "Stretch", "Weak fit"):
        verdict = "Strong fit" if score >= 85 else "Good fit" if score >= 65 else "Stretch" if score >= 45 else "Weak fit"
    return {"score": score, "verdict": verdict, "matches": _strs(data.get("matches"), 4),
            "gaps": _strs(data.get("gaps"), 4), "keywords_to_add": _strs(data.get("keywords_to_add"), 6),
            "tweaks": _strs(data.get("tweaks"), 3), "lead_with": str(data.get("lead_with") or "").strip()}


# ---------------------------------------------------------------- running it

def analyze_jobs(jobs, call=None, describe=None, sleep=time.sleep):
    """Adds job['analysis'] to as many jobs as the limits allow. Never raises for a single job."""
    resume = os.environ.get("RESUME_TEXT", "").strip()
    if call is None and not (resume and os.environ.get("GEMINI_API_KEY", "").strip()):
        print("Match analysis is off (add GEMINI_API_KEY and RESUME_TEXT secrets to turn it on).")
        return
    call = call or call_gemini
    describe = describe or fetch_description
    started = time.time()
    done = 0
    for j in jobs[:MAX_JOBS_PER_RUN]:
        if time.time() - started > TIME_BUDGET_SECONDS:
            print("Match analysis stopped early to stay within the time budget.")
            break
        try:
            jd = describe(j)
            if len(jd) < 150:
                print(f"  Skipped {j['title']} at {j['company']}: couldn't read the description.")
                continue
            prompt = PROMPT.format(
                resume=resume[:MAX_RESUME_CHARS], title=j["title"], company=j["company"],
                location_line=f"LOCATION: {j['location']}" if j.get("location") else "", jd=jd)
            j["analysis"] = call(prompt)
            done += 1
            sleep(SECONDS_BETWEEN_CALLS)
        except RateLimited as e:
            print(f"Match analysis stopped: {e}.")
            break
        except Exception as e:
            print(f"  Match analysis failed for {j['title']} at {j['company']}: {e}")
    print(f"Match analysis: {done} of {min(len(jobs), MAX_JOBS_PER_RUN)} roles analyzed.")


# ---------------------------------------------------------------- showing it in the email

def _colors(score):
    if score >= 85:
        return "#0f7b3f", "#e3f5ea"
    if score >= 65:
        return "#2f4bd8", "#e8ecfd"
    if score >= 45:
        return "#8a5a00", "#fdf1d9"
    return "#5d6478", "#eceef3"


def render_html(a):
    fg, bg = _colors(a["score"])

    def block(label, items):
        if not items:
            return ""
        lis = "".join(f"<li>{escape(i)}</li>" for i in items)
        return (f'<div style="margin-top:6px"><span style="font-weight:600">{label}</span>'
                f'<ul style="margin:2px 0 0 18px;padding:0">{lis}</ul></div>')

    lead = (f'<div style="margin-top:6px"><span style="font-weight:600">Lead with:</span> '
            f'{escape(a["lead_with"])}</div>') if a["lead_with"] else ""
    kw = (f'<div style="margin-top:6px"><span style="font-weight:600">Keywords to work in:</span> '
          f'{escape(", ".join(a["keywords_to_add"]))}</div>') if a["keywords_to_add"] else ""
    return (f'<div style="margin-top:10px;padding:10px 12px;background:#f6f7fb;border-radius:8px;'
            f'font-size:13px;color:#3b4255">'
            f'<span style="display:inline-block;background:{bg};color:{fg};font-weight:700;'
            f'padding:2px 8px;border-radius:6px">Fit {a["score"]}/100 &middot; {escape(a["verdict"])}</span>'
            f'{block("Matches", a["matches"])}{block("Gaps", a["gaps"])}{lead}'
            f'{block("Resume tweaks", a["tweaks"])}{kw}</div>')


def render_text(a):
    lines = [f"  Resume fit: {a['score']}/100 ({a['verdict']})"]
    for label, items in (("Matches", a["matches"]), ("Gaps", a["gaps"]), ("Resume tweaks", a["tweaks"])):
        if items:
            lines.append(f"  {label}: " + "; ".join(items))
    if a["lead_with"]:
        lines.append(f"  Lead with: {a['lead_with']}")
    if a["keywords_to_add"]:
        lines.append("  Keywords to work in: " + ", ".join(a["keywords_to_add"]))
    return "\n".join(lines)

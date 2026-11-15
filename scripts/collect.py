#!/usr/bin/env python3
"""Collect junior economics-of-AI vacancies in the UK (or remote) for the
Economics of AI (7SSPN409) jobs board.

The script reads the employer list in sources.json, downloads each employer's
public job feed, keeps the vacancies that pass the filters below, and writes
site/data/jobs.json, which the web page reads.

It uses only the Python standard library, so it runs on a plain GitHub Actions
runner with nothing to install.

The filters, in the order they are applied to each vacancy:
  1. Place       - based in the UK, or remote and open to people in the UK.
  2. Seniority   - no senior titles; no advert asking for more than
                   MAX_YEARS years of experience; no PhD requirement.
  3. Role        - an economics, policy, research or analysis role.
  4. AI focus    - at employers that are not AI organisations, the advert
                   itself has to be about AI, technology or digital markets.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.json"
OUT_FILE = ROOT / "site" / "data" / "jobs.json"

MAX_YEARS = 3            # most experience an advert may ask for
NEW_DAYS = 7             # a vacancy counts as "new" for this many days
USER_AGENT = "Mozilla/5.0 (compatible; econ-ai-jobs-board/1.0)"
TODAY = dt.datetime.now(dt.timezone.utc).date()


# ---------------------------------------------------------------- fetching --

def fetch(url: str, data: bytes | None = None, headers: dict | None = None,
          timeout: int = 45, tries: int = 3) -> bytes:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            last = err
            if err.code in (400, 401, 403, 404, 410):
                break
        except Exception as err:  # timeouts, resets, DNS
            last = err
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def fetch_json(url: str, **kw):
    return json.loads(fetch(url, **kw).decode("utf-8", "replace"))


def to_text(markup: str | None) -> str:
    """Turn HTML (possibly entity-escaped HTML) into plain text."""
    if not markup:
        return ""
    s = html.unescape(markup)
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<\s*(br|/p|/div|/li|/h[1-6]|/tr|/ul|/ol)\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v ​]+", " ", s)
    s = re.sub(r" ?\n ?", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def iso_date(value) -> str | None:
    """Best-effort conversion of a feed's date value to YYYY-MM-DD."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        if value > 1e11:      # milliseconds
            value /= 1000
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).date().isoformat()
    s = str(value).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    try:                      # RSS dates: "Mon, 09 Nov 2026 10:00:00 +0000"
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).date().isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------- adapters --
# Each adapter returns a list of dicts with the keys:
#   sid, title, location, url, posted, description, employment, salary,
#   remote (True/False/None), country (ISO name or ""), employer (optional),
#   lazy (optional function that returns the description when needed)

def raw(**kw) -> dict:
    base = dict(sid="", title="", location="", url="", posted=None,
                description="", employment="", salary="", remote=None,
                country="", employer="", lazy=None, closes=None)
    base.update(kw)
    return base


def greenhouse(src: dict) -> list[dict]:
    host = src.get("host", "boards-api.greenhouse.io")
    data = fetch_json(f"https://{host}/v1/boards/{src['token']}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        out.append(raw(
            sid=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            location=((j.get("location") or {}).get("name") or "").strip(),
            url=j.get("absolute_url") or "",
            posted=iso_date(j.get("first_published")),
            description=to_text(j.get("content")),
        ))
    return out


def ashby(src: dict) -> list[dict]:
    data = fetch_json("https://api.ashbyhq.com/posting-api/job-board/"
                      f"{urllib.parse.quote(src['token'])}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""]
        for extra in j.get("secondaryLocations") or []:
            locs.append(extra.get("location") or "")
        address = ((j.get("address") or {}).get("postalAddress") or {})
        comp = j.get("compensation") or {}
        out.append(raw(
            sid=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            location=" | ".join(x for x in locs if x),
            url=j.get("jobUrl") or j.get("applyUrl") or "",
            posted=iso_date(j.get("publishedAt")),
            description=j.get("descriptionPlain") or to_text(j.get("descriptionHtml")),
            employment=j.get("employmentType") or "",
            salary=comp.get("compensationTierSummary") or "",
            remote=j.get("isRemote"),
            country=address.get("addressCountry") or "",
        ))
    return out


def lever(src: dict) -> list[dict]:
    host = src.get("host", "api.lever.co")
    data = fetch_json(f"https://{host}/v0/postings/{src['token']}?mode=json"
                      + src.get("query", ""))
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locs = cats.get("allLocations") or [cats.get("location") or ""]
        parts = [j.get("descriptionPlain") or ""]
        for block in j.get("lists") or []:
            parts.append((block.get("text") or "") + "\n" + to_text(block.get("content")))
        parts.append(j.get("additionalPlain") or "")
        out.append(raw(
            sid=str(j.get("id")),
            title=(j.get("text") or "").strip(),
            location=" | ".join(x for x in locs if x),
            url=j.get("hostedUrl") or "",
            posted=iso_date(j.get("createdAt")),
            description="\n".join(p for p in parts if p),
            employment=cats.get("commitment") or "",
            remote=True if j.get("workplaceType") == "remote" else None,
            country=j.get("country") or "",
        ))
    return out


def workable(src: dict) -> list[dict]:
    data = fetch_json("https://apply.workable.com/api/v1/widget/accounts/"
                      f"{src['token']}?details=true")
    out = []
    for j in data.get("jobs", []):
        place = ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        out.append(raw(
            sid=str(j.get("shortcode")),
            title=(j.get("title") or "").strip(),
            location=place,
            url=j.get("url") or j.get("shortlink") or "",
            posted=iso_date(j.get("published_on") or j.get("created_at")),
            description=to_text(j.get("description")),
            employment=j.get("employment_type") or "",
            remote=bool(j.get("telecommuting")) or None,
            country=j.get("country") or "",
        ))
    return out


def recruitee(src: dict) -> list[dict]:
    data = fetch_json(f"https://{src['token']}.recruitee.com/api/offers/")
    out = []
    for j in data.get("offers", []):
        out.append(raw(
            sid=str(j.get("id")),
            title=(j.get("title") or "").strip(),
            location=j.get("location") or ", ".join(
                x for x in (j.get("city"), j.get("country")) if x),
            url=j.get("careers_url") or "",
            posted=iso_date(j.get("published_at") or j.get("created_at")),
            description=to_text(j.get("description")) + "\n" + to_text(j.get("requirements")),
            employment=j.get("employment_type_code") or "",
            remote=bool(j.get("remote")) or None,
            country=j.get("country") or "",
        ))
    return out


def smartrecruiters(src: dict) -> list[dict]:
    company = src["token"]
    out, offset = [], 0
    while True:
        data = fetch_json("https://api.smartrecruiters.com/v1/companies/"
                          f"{company}/postings?limit=100&offset={offset}"
                          + (f"&country={src['country']}" if src.get("country") else ""))
        items = data.get("content", [])
        for j in items:
            loc = j.get("location") or {}
            place = ", ".join(x for x in (loc.get("city"), loc.get("country", "").upper()) if x)

            def lazy(job, jid=j.get("id")):
                detail = fetch_json("https://api.smartrecruiters.com/v1/companies/"
                                    f"{company}/postings/{jid}")
                sections = ((detail.get("jobAd") or {}).get("sections") or {})
                return "\n".join(to_text((sections.get(k) or {}).get("text"))
                                 for k in ("jobDescription", "qualifications",
                                           "additionalInformation"))

            out.append(raw(
                sid=str(j.get("id")),
                title=(j.get("name") or "").strip(),
                location=place,
                url=f"https://jobs.smartrecruiters.com/{company}/{j.get('id')}",
                posted=iso_date(j.get("releasedDate")),
                employment=((j.get("typeOfEmployment") or {}).get("label") or ""),
                remote=bool(loc.get("remote")) or None,
                country="GB" if (loc.get("country") or "").lower() == "gb" else (loc.get("country") or ""),
                lazy=lazy,
            ))
        offset += len(items)
        if not items or offset >= data.get("totalFound", 0) or offset >= 1000:
            break
    return out


def workday(src: dict) -> list[dict]:
    """Workday career sites. `base` looks like
    https://tenant.wd3.myworkdayjobs.com and `site` is the career-site name."""
    base, tenant, site = src["base"].rstrip("/"), src["tenant"], src["site"]
    api = f"{base}/wday/cxs/{tenant}/{site}"
    out, offset, total = [], 0, 0
    while True:
        body = {"limit": 20, "offset": offset, "searchText": src.get("search", "")}
        if src.get("facets"):
            body["appliedFacets"] = src["facets"]
        data = fetch_json(api + "/jobs", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json",
                                   "Accept": "application/json"})
        items = data.get("jobPostings", [])
        for j in items:
            path = j.get("externalPath") or ""

            def lazy(job, p=path):
                info = (fetch_json(api + p, headers={"Accept": "application/json"})
                        .get("jobPostingInfo") or {})
                job["posted"] = iso_date(info.get("startDate")) or job.get("posted")
                job["closes"] = iso_date(info.get("endDate")) or job.get("closes")
                return to_text(info.get("jobDescription"))

            where_text = j.get("locationsText") or ""
            if re.match(r"\d+ Locations?$", where_text):
                # Multi-site adverts hide the place; the first one is in the address.
                m = re.match(r"/job/([^/]+)/", path)
                where_text = m.group(1).replace("-", " ") if m else where_text
            out.append(raw(
                sid=path.rsplit("/", 1)[-1],
                title=(j.get("title") or "").strip(),
                location=where_text,
                url=f"{base}/en-US/{site}{path}",
                lazy=lazy,
            ))
        offset += len(items)
        total = data.get("total") or total      # later pages report 0
        if not items or offset >= total or offset >= 400:
            break
    return out


def oracle(src: dict) -> list[dict]:
    """Oracle Recruiting Cloud career sites (e.g. the Bank of England).
    `base` is the host and `site` the career-site number."""
    base, site = src["base"].rstrip("/"), src["site"]
    api = base + "/hcmRestApi/resources/latest/"
    data = fetch_json(api + "recruitingCEJobRequisitions?onlyData=true"
                      "&expand=requisitionList.secondaryLocations"
                      f"&finder=findReqs;siteNumber={site},limit=200,sortBy=POSTING_DATES_DESC")
    out = []
    for j in ((data.get("items") or [{}])[0].get("requisitionList") or []):
        jid = str(j.get("Id"))

        def lazy(job, jid=jid):
            detail = fetch_json(api + "recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
                                f"&finder=ById;Id=%22{jid}%22,siteNumber={site}")
            item = (detail.get("items") or [{}])[0]
            return "\n".join(to_text(item.get(k)) for k in (
                "ExternalDescriptionStr", "ExternalQualificationsStr",
                "ExternalResponsibilitiesStr") if item.get(k))

        out.append(raw(
            sid=jid,
            title=(j.get("Title") or "").strip(),
            location=j.get("PrimaryLocation") or "",
            url=f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{jid}",
            posted=iso_date(j.get("PostedDate")),
            closes=iso_date(j.get("PostingEndDate")),
            lazy=lazy,
        ))
    return out


def aisafety(src: dict) -> list[dict]:
    """The AISafety.com jobs board, which lists AI safety and governance roles
    at many organisations. Each card states the experience level and degree
    asked for, which is used here in place of reading the full advert."""
    doc = fetch(src["url"], timeout=90).decode("utf-8", "replace")
    out = []
    for card in re.findall(r'(?s)<li id="rec[^"]*" class="card.*?</li>', doc):
        link = re.search(r'(?s)<h3>\s*<a href="([^"]+)"[^>]*>(.*?)</a>', card)
        if not link:
            continue
        url = html.unescape(link.group(1))
        url = re.sub(r"[?&]utm_[a-z]+=[^&]*", "", url).rstrip("?&")
        title = to_text(re.sub(r"(?s)<span class=\"visually-hidden\">.*?</span>", "", link.group(2)))
        facts = {}
        for p in re.findall(r'(?s)<p class="paragraph-xs[^"]*">(.*?)</p>', card):
            line = to_text(p)
            key, sep, value = line.partition(":")
            if sep:
                facts[key.strip().lower()] = value.strip()
            elif line.lower().startswith("posted "):
                facts["posted"] = line[7:]
        experience = facts.get("min experience", "")
        if experience and not re.search(r"entry|junior|multiple", experience, re.I):
            continue
        if "doctoral" in facts.get("required degree", "").lower():
            continue
        skills = facts.get("skillset", "")
        if skills and not re.search(r"policy|strategy|research|data|finance", skills, re.I):
            continue        # operations, engineering, security and similar
        blurb = re.search(r'(?s)<p class="paragraph-small[^"]*">(.*?)</p>', card)
        posted = None
        m = re.match(r"(\d{1,2}) (\w+) (\d{4})", facts.get("posted", ""))
        if m and m.group(2).lower() in MONTHS:
            posted = f"{m.group(3)}-{MONTHS.index(m.group(2).lower()) + 1:02d}-{int(m.group(1)):02d}"
        out.append(raw(
            sid=re.search(r'id="(rec[^"]*)"', card).group(1),
            title=title,
            employer=facts.get("organization", ""),
            location=facts.get("location", ""),
            url=url,
            posted=posted,
            description=to_text(blurb.group(1)) if blurb else "",
            employment=facts.get("role type", ""),
            salary=facts.get("salary", ""),
            level_hint="Entry level" if re.search(r"entry", experience, re.I)
            else ("1 to 4 years' experience" if re.search(r"junior", experience, re.I) else None),
        ))
    return out


def rss(src: dict) -> list[dict]:
    """Generic RSS or Atom feed. One item is one vacancy."""
    root = ET.fromstring(fetch(src["url"]))
    out = []
    for item in root.iter():
        tag = item.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in item:
            name = child.tag.rsplit("}", 1)[-1].lower()
            value = (child.text or "").strip()
            if name == "link" and not value:
                value = child.attrib.get("href", "")
            fields.setdefault(name, value)
        # Teamtailor feeds nest the place: locations > location > city, country
        places = [(n.text or "").strip() for n in item.iter()
                  if n.tag.rsplit("}", 1)[-1].lower() in ("city", "country") and (n.text or "").strip()]
        if (fields.get("remotestatus") or "").lower() in ("fully", "remote"):
            places.append("Remote")
        title = fields.get("title", "")
        out.append(raw(
            sid=fields.get("guid") or fields.get("id") or fields.get("link", ""),
            title=to_text(title),
            location=fields.get("location", "") or ", ".join(dict.fromkeys(places))
            or src.get("default_location", ""),
            url=fields.get("link", ""),
            posted=iso_date(fields.get("pubdate") or fields.get("published")
                            or fields.get("updated") or fields.get("date")),
            description=to_text(fields.get("description") or fields.get("summary")
                                or fields.get("content") or ""),
            employer=fields.get("employer", "") or fields.get("company", ""),
        ))
    return out


def pinpoint(src: dict) -> list[dict]:
    """Pinpoint careers sites: https://{token}.pinpointhq.com (or a custom
    domain given as `base`)."""
    base = src.get("base", f"https://{src.get('token')}.pinpointhq.com").rstrip("/")
    data = fetch_json(base + "/postings.json")
    out = []
    for j in data.get("data", []):
        loc = j.get("location") or {}
        place_name = loc.get("name") or ", ".join(
            x for x in (loc.get("city"), loc.get("province")) if x)
        workplace = j.get("workplace_type_text") or ""
        text = "\n".join(to_text(j.get(k)) for k in (
            "description", "key_responsibilities", "skills_knowledge_expertise", "benefits")
            if isinstance(j.get(k), str))
        out.append(raw(
            sid=str(j.get("id") or j.get("path")),
            title=(j.get("title") or "").strip(),
            location=place_name or src.get("default_location", ""),
            url=j.get("url") or (base + (j.get("path") or "")),
            description=text.strip(),
            employment=j.get("employment_type_text") or "",
            remote=True if "remote" in workplace.lower() else None,
            closes=iso_date(j.get("deadline_at")),
        ))
    return out


def page_text_after_heading(doc: str) -> tuple[str, str]:
    """Title (first <h1>) and the text that follows it on an advert page."""
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", doc)
    title = to_text(m.group(1)) if m else ""
    body = doc[m.end():] if m else doc
    body = re.sub(r"(?is)<(footer|nav|form)[^>]*>.*?</\1>", " ", body)
    return title, to_text(body)


def html_links(src: dict) -> list[dict]:
    """For employers with no feed: read the vacancies page, follow every link
    whose address matches `link_pattern`, and take the title and text from
    each advert page. The place comes from `default_location`."""
    doc = fetch(src["url"]).decode("utf-8", "replace")
    pattern = re.compile(src["link_pattern"])
    skip = re.compile(src["exclude_pattern"]) if src.get("exclude_pattern") else None
    links = []
    for href in re.findall(r'href="([^"#?]+)', doc):
        if pattern.search(href) and not (skip and skip.search(href)):
            full = urllib.parse.urljoin(src["url"], href)
            if full not in links:
                links.append(full)
    out = []
    for link in links[:40]:
        try:
            title, text = page_text_after_heading(fetch(link).decode("utf-8", "replace"))
        except Exception as err:
            print(f"    could not read {link}: {err}")
            continue
        out.append(raw(
            sid=link.rstrip("/").rsplit("/", 1)[-1],
            title=title,
            location=src.get("default_location", ""),
            url=link,
            description=text,
        ))
    return out


def google_careers(src: dict) -> list[dict]:
    """Google's careers search (used for Google DeepMind). The search address
    in `url` already restricts results to the United Kingdom."""
    out, seen = [], set()
    for page in range(1, 9):
        doc = fetch(f"{src['url']}&page={page}").decode("utf-8", "replace")
        found = 0
        for chunk in re.split(r"<h3[ >]", doc)[1:]:
            link = re.search(r"jobs/results/(\d+)-[a-z0-9-]+", chunk)
            if not link or link.group(1) in seen:
                continue
            seen.add(link.group(1))
            found += 1
            head, _, rest = chunk.partition("</h3>")
            title = to_text("<h3 " + head)
            title = re.sub(r",\s*(Google )?DeepMind$", "", title).strip()
            place_match = re.search(r">([^<>;]{2,40}, UK)<", rest)
            cut = rest.find("jobs/results/")
            out.append(raw(
                sid=link.group(1),
                title=title,
                location=place_match.group(1) if place_match
                else src.get("default_location", "United Kingdom"),
                url="https://www.google.com/about/careers/applications/" + link.group(0),
                description=to_text(rest[:cut] if cut > 0 else rest),
            ))
        if not found:
            break
    return out


ADAPTERS = {
    "greenhouse": greenhouse, "ashby": ashby, "lever": lever,
    "workable": workable, "recruitee": recruitee,
    "smartrecruiters": smartrecruiters, "workday": workday, "rss": rss,
    "pinpoint": pinpoint, "html": html_links, "google": google_careers,
    "oracle": oracle, "aisafety": aisafety,
}


# ----------------------------------------------------------------- filters --

def rx(pattern: str, flags: int = re.I) -> re.Pattern:
    return re.compile(pattern, flags)


# --- 1. Place ---------------------------------------------------------------
# Place names that look British but are not.
FALSE_FRIENDS = rx(
    r"new york|new south wales|london,? (on|ontario|ky|oh)\b|"
    r"cambridge,? (ma|mass|massachusetts|us|usa|md|on)\b|"
    r"birmingham,? (al|alabama|mi)\b|manchester,? (nh|ct|mo)\b|"
    r"durham,? (nc|north carolina|nh)\b|oxford,? (ms|oh|ohio|mississippi)\b|"
    r"bristol,? (ct|tn|va|ri)\b|reading,? (pa|ma)\b|bath,? (me|maine)\b|"
    r"newcastle,? (nsw|australia)\b|york,? (pa|me)\b|belfast,? (me|maine)\b|"
    r"newport,? (ri|ca|ky|or)\b|richmond,? (va|ca)\b|"
    r"ukraine")
UK_PLACES = rx(
    r"\b(united kingdom|u\.?k\.?|great britain|britain|england|scotland|wales|"
    r"northern ireland|london|cambridge|oxford|manchester|edinburgh|bristol|"
    r"birmingham|leeds|glasgow|cardiff|belfast|sheffield|liverpool|nottingham|"
    r"newcastle|reading|darlington|salford|coventry|warwick|york|bath|brighton|"
    r"exeter|southampton|durham|st\.? andrews|aberdeen|swindon|newport|"
    r"titchfield|harwell|milton keynes|leicester|norwich|canary wharf|"
    r"westminster|whitehall)\b")
LONDON = rx(r"\b(london|canary wharf|westminster|whitehall)\b")
REMOTE_WORDS = rx(r"\b(remote|anywhere|worldwide|global|distributed|"
                  r"work from home|home[- ]based|virtual)\b")
# After removing the word "remote", what is left has to be one of these (or
# nothing) for a remote job to count as open to someone living in the UK.
REMOTE_OK_REGIONS = rx(r"\b(emea|europe|european|eu|global\w*|worldwide|anywhere|"
                       r"international|gmt|bst|utc|cet)\b")
# Remote adverts whose text says only people in the United States can apply.
US_ONLY = rx(r"(must|need to|required to|have to) (be|reside|live|be based|be located) "
             r"(in|within) the (u\.?s\.?|united states)|"
             r"(u\.?s\.?|united states)[- ]based (candidates|applicants) only|"
             r"(authori[sz]ed|eligible|right) to work in the (u\.?s\.?|united states)|"
             r"u\.?s\.? citizen(ship)? (is )?required")
UK_COUNTRY_CODES = {"gb", "uk", "united kingdom", "great britain", "england",
                    "scotland", "wales", "northern ireland"}


def place(job: dict) -> dict | None:
    """Return {'where': 'London'|'UK'|'Remote', 'label': str} or None."""
    original = (job["location"] or "").strip()
    cleaned = FALSE_FRIENDS.sub(" ", original)
    country = (job.get("country") or "").strip().lower()
    is_remote = bool(job.get("remote")) or bool(REMOTE_WORDS.search(cleaned))
    parts = [p.strip() for p in re.split(r"\s*[|;/·•]\s*|\s+or\s+|\s+and\s+", original)
             if p.strip()]

    if UK_PLACES.search(cleaned) or (country in UK_COUNTRY_CODES and not cleaned):
        uk_parts = [p for p in parts if UK_PLACES.search(FALSE_FRIENDS.sub(" ", p))]
        label = " · ".join(dict.fromkeys(uk_parts)) or original or "United Kingdom"
        if len(parts) > len(uk_parts) > 0:
            label += f" (+{len(parts) - len(uk_parts)} other)"
        if is_remote and not REMOTE_WORDS.search(label):
            label += " · remote possible"
        where = "London" if LONDON.search(label) else "UK"
        if is_remote and not LONDON.search(label) and REMOTE_WORDS.search(label) \
                and not re.sub(UK_PLACES, "", REMOTE_WORDS.sub("", label)).strip(" ,-()·:"):
            where = "Remote"
        return {"where": where, "label": label}

    if is_remote:
        rest = REMOTE_WORDS.sub(" ", cleaned)
        rest = re.sub(r"[\s,\-–—()/:|·]+|\b(only|based|friendly|first|fully|"
                      r"hybrid|or|in|within|time ?zones?|role|position)\b",
                      " ", rest, flags=re.I).strip()
        if not rest:
            return {"where": "Remote", "label": "Remote"}
        if REMOTE_OK_REGIONS.search(rest):
            return {"where": "Remote", "label": original}
    return None


# --- 2. Seniority -----------------------------------------------------------
SENIOR_TITLE = rx(
    r"\b(senior|sr\.?|lead|principal|head|director|chief|vp|vice[- ]president|"
    r"staff|partner|professor|chair|executive director|managing|expert|"
    r"post[- ]?doc\w*|lecturer|reader|"
    r"distinguished|fellow, senior|superintendent|president|c[efot]o|"
    r"general counsel|deputy director|grade 6|g6|scs\d?)\b")
# "Manager" titles usually need several years; they are kept only when the
# advert itself states a requirement of MAX_YEARS years or less.
MANAGER_TITLE = rx(r"\b(manager|management lead|team leader)\b")
# Entry-level economics titles (used only for employers marked "economics").
ECON_ENTRY = rx(
    r"\b(econom\w*|econometric\w*|competition|antitrust|regulat\w+|polic(y|ies)|"
    r"analyst|graduate|intern(ship)?|placement|consulting associate|"
    r"research (analyst|assistant|associate))\b")
JUNIOR_TITLE = rx(
    r"\b(graduate|junior|jr\.?|intern(ship)?|trainee|assistant|entry[- ]level|"
    r"apprentice(ship)?|early[- ]career|pre[- ]?doc(toral)?|placement|"
    r"summer|new grad|associate|analyst|fellow(ship)?|scholar|officer)\b")

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
                "fifteen": 15}
_NUM = r"(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")"
YEARS = rx(r"(?<![\d£$€.,])" + _NUM + r"\s*(?:\+|plus)?\s*"
           r"(?:(?:-|–|—|to|or)\s*" + _NUM + r"\s*\+?\s*)?"
           r"(?:years?|yrs?)\b")
EXPERIENCE_NEAR = rx(r"experience|experienced|track record|background in|"
                     r"working in|worked in|work in|professional|post[- ]?graduate|"
                     r"post[- ]?qualif|in a similar|relevant")
PHD = rx(r"\b(ph\.?d|d\.?phil|doctorate|doctoral degree)\b")
PHD_SOFTENERS = rx(r"\bor\b|equivalent|master|msc|\bma\b|preferr|desirable|"
                   r"advantage|bonus|nice to have|ideally|plus|working towards|"
                   r"not required|optional|students?|candidates?|program|"
                   r"during|pursuing|enrolled|complet(ing|ion)")
PHD_REQUIRED = rx(r"require|must|essential|hold|minimum|necessary|"
                  r"you (have|will have)|qualification")


def to_int(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token.lower()]


def years_required(text: str) -> int | None:
    """Largest minimum number of years of experience the advert asks for."""
    found = []
    for m in YEARS.finditer(text):
        low = to_int(m.group(1))
        if low > 15:
            continue
        # Look for "experience" in the same line, close to the number.
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line_end = len(text) if line_end < 0 else line_end
        near = text[max(line_start, m.start() - 70): min(line_end, m.end() + 110)]
        if EXPERIENCE_NEAR.search(near):
            found.append(low)
    return max(found) if found else None


def phd_required(text: str) -> bool:
    for sentence in re.split(r"(?<=[.!?;])\s+|\n", text):
        if PHD.search(sentence) and PHD_REQUIRED.search(sentence) \
                and not PHD_SOFTENERS.search(sentence):
            return True
    return False


# --- 3. Role ----------------------------------------------------------------
# Titles that are about economics or policy outright. These override the
# "not for this board" list below (but never the seniority rules).
CORE_ROLE = rx(
    r"\b(econom\w*|econometric\w*|polic(y|ies)|societal|labou?r market\w*|"
    r"governance|social scien\w*|public affairs|government (affairs|relations)|"
    r"geopolitic\w*|international (affairs|relations)|regulat\w+|competition|"
    r"antitrust|forecast\w*|think tank|political|"
    r"macro\w*|productivity|industrial strategy|"
    r"impact (assessment|evaluation|analysis))\b")
# ...except these, which are never right for this board even when the title
# also says "policy" or "economics" (e.g. "Recruiter, Policy").
NEVER = rx(
    r"\b(engineer\w*|developer|architect|counsel|legal|lawyer|solicitor|"
    r"recruit\w*|sales|account (executive|manager)|marketing|design(er)?|"
    r"customer|solutions?|deployment|data governance|corporate governance|"
    r"account management|business development|"
    r"insurance|claims|underwrit\w*|nurse|clinical|payroll|accountant)\b")
# At AI companies, the only non-economics titles that are kept.
COMPANY_ROLE = rx(
    r"\b(pricing|market (research|analysis|intelligence|insights?)|"
    r"(strategy|research|insights?|market|operations research) analyst|"
    r"strategy (associate|intern)|economic\w*|social impact|"
    r"research (assistant|associate) (,|-|–)? ?(econom|polic|societ))")
# Titles that might be relevant, depending on the rest of the advert.
BROAD_ROLE = rx(
    r"\b(research(er)?|analyst|analysis|analytics|strategy|strategist|"
    r"fellow(ship)?|scholar|intern(ship)?|graduate|consultant|associate|"
    r"adviser|advisor|officer|data scien\w*|statistic\w*|evaluation|insight\w*|"
    r"programme|program|generalist|chief of staff|operations|writer|editor|"
    r"communications|grants?|grantmaking|investigat\w*|pricing|markets?)\b")
# Roles this board is not for.
OFF_TOPIC = rx(
    r"\b(engineer\w*|developer|software|devops|sre|architect|infrastructure|"
    r"full[- ]?stack|front[- ]?end|back[- ]?end|machine learning|\bml\b|"
    r"research scientists?|member of technical staff|interpretability|"
    r"red team\w*|security|cyber\w*|sales|account (executive|manager|director)|"
    r"business development|bdr|sdr|customer|solutions?|recruit\w*|talent|"
    r"people (partner|ops|operations)|human resources|\bhr\b|payroll|"
    r"account(ant|ing|s)|bookkeep\w*|tax|treasury analyst|audit\w*|legal|"
    r"counsel|lawyer|attorney|paralegal|solicitor|design(er)?|ux|brand|"
    r"marketing|seo|social media|events?|video|creative|producer|"
    r"executive assistant|personal assistant|\bpa\b|receptionist|"
    r"office manager|facilities|workplace|it support|helpdesk|technician|"
    r"hardware|robotic\w*|chip|silicon|electrical|mechanical|datacenter|"
    r"data cent(er|re) (technician|operations)|supply chain|procurement|"
    r"physician|clinical|nurse|biolog\w*|chemist\w*|physics|wet lab|"
    r"annotat\w*|linguist\w*|translator|tutor|voice|actor|"
    r"gtm|go[- ]to[- ]market|expressions? of interest|cx|teacher|"
    r"technical (analyst|specialist)|m365|microsoft 365|service desk|"
    r"systems? (analyst|administrator)|talent pool|"
    r"product manager|product owner|scrum|qa|quality assurance|tester|"
    r"finance (manager|business partner)|fp&a|controller|"
    r"partnerships?|deployment|implementation|enablement|support)\b")

# --- 4. AI focus ------------------------------------------------------------
AI_CASED = re.compile(r"\b(AI|A\.I\.|GenAI|AGI|LLMs?|ML)\b")
AI_WORDS = rx(
    r"\b(artificial intelligence|machine learning|generative|"
    r"large language models?|language models?|frontier models?|automation|"
    r"algorithm\w*|digital (markets?|econom\w+|regulation|technolog\w+|competition|policy)|"
    r"tech(nology)? (policy|regulation|sector|markets?)|emerging technolog\w+|"
    r"data cent(er|re)s?|semiconductors?|compute|robotics|"
    r"online platforms?|big tech|technological change|future of work)\b")
# Sentences that mention AI only because of how the employer recruits.
RECRUITING_NOISE = rx(r"applic|candidate|interview|recruit|hiring|cover letter|"
                      r"cv\b|resume|assessment|screen")


def ai_mentions(text: str) -> int:
    count = 0
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        hits = len(AI_CASED.findall(sentence)) + len(AI_WORDS.findall(sentence))
        if hits and not RECRUITING_NOISE.search(sentence):
            count += hits
    return count


# --- Labels -----------------------------------------------------------------
def kind(title: str, employment: str) -> str:
    t = f"{title} {employment}".lower()
    if re.search(r"\bintern(ship)?s?\b|placement|summer (analyst|associate)|work experience", t):
        return "Internship"
    if re.search(r"\bfellow(ship)?s?\b|scholar|residen(t|cy)|\bprogramme\b.*\b(talent|graduate)|"
                 r"graduate (scheme|programme|program)|pre[- ]?doc|research assistant|"
                 r"visiting|studentship", t):
        return "Fellowship or programme"
    return "Job"


def level(title: str, years: int | None) -> str:
    if re.search(r"\b(graduate|junior|jr|intern(ship)?|trainee|entry[- ]level|"
                 r"apprentice|early[- ]career|pre[- ]?doc|placement|new grad|"
                 r"assistant)\b", title, re.I) or years in (0, 1):
        return "Entry level"
    if years in (2, 3):
        return f"{years}+ years' experience"
    return "Experience not stated"


SALARY = re.compile(
    r"£\s?\d{2,3}(?:[,.]\d{3}|k)(?:\s?(?:-|–|—|to)\s?£?\s?\d{2,3}(?:[,.]\d{3}|k))?"
    r"(?:\s?(?:per annum|per year|pa|p\.a\.|a year))?", re.I)
MONTHS = ["january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december"]
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|" \
       r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
# A deadline is a date that follows a closing phrase in the same sentence.
# "until" only counts next to an application word, so that "a fixed-term
# contract until 31 March" is not read as a deadline.
CLOSING = rx(r"(?:clos(?:es|ing)|deadline|apply by|applications? (?:are )?due|"
             r"applications? (?:will |have |are |were )?(?:now )?closed?|submit(?:ted)? by|"
             r"(?:open|accept\w+|considered|received|apply|applications?)[^.\n]{0,40}?\buntil)"
             r"[^.\n]{0,70}?(?:(?<!\d)(\d{1,2})(?:st|nd|rd|th)?(?: of)?\s+" + _MON +
             r"\.?,?\s*(\d{4})?|" + _MON + r"\.?\s+(\d{1,2})(?!\d)(?:st|nd|rd|th)?,?\s*(\d{4})?)")


def closing_date(text: str) -> str | None:
    """The application deadline stated in an advert, as YYYY-MM-DD. A deadline
    that has already passed is returned too, so the advert can be dropped.
    When an advert gives several dates, the next one still open wins."""
    found = []
    for m in CLOSING.finditer(text or ""):
        if m.group(1):
            day, mon, year = m.group(1), m.group(2), m.group(3)
        else:
            mon, day, year = m.group(4), m.group(5), m.group(6)
        month = next(i for i, name in enumerate(MONTHS, 1) if name.startswith(mon.lower()[:3]))
        try:
            if year:
                date = dt.date(int(year), month, int(day))
            else:
                date = dt.date(TODAY.year, month, int(day))
                if date < TODAY - dt.timedelta(days=60):
                    date = dt.date(TODAY.year + 1, month, int(day))
        except ValueError:
            continue
        if TODAY - dt.timedelta(days=180) <= date <= TODAY + dt.timedelta(days=240):
            found.append(date)
    if not found:
        return None
    ahead = [d for d in found if d >= TODAY]
    return (min(ahead) if ahead else max(found)).isoformat()


ROLE_HEADINGS = rx(r"^(about the (role|job|position|team|opportunity|fellowship|programme|program)|"
                   r"the (role|opportunity|position|job)|role (summary|overview|description|purpose)|"
                   r"job (summary|description|purpose|overview)|what you(’|')?ll (do|be doing)|"
                   r"overview|position (summary|overview)|summary|purpose of the role|"
                   r"your role|responsibilities|in this role)\b[:\s]*", re.I | re.M)


def summary(text: str, limit: int = 260) -> str:
    """A short extract, starting where the advert describes the role."""
    m = ROLE_HEADINGS.search(text)
    body = text[m.end():] if m else text
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) <= limit:
        return body
    cut = body[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    cut = cut[: stop + 1] if stop > limit * 0.5 else cut[: cut.rfind(" ")] + "…"
    return cut


# ----------------------------------------------------------------- pipeline --

def assess(job: dict, src: dict) -> tuple[dict | None, str]:
    """Apply the filters. Returns (record, "") or (None, reason dropped)."""
    title = job["title"]
    if not title or not job["url"]:
        return None, "incomplete"

    if src.get("title_must_match") and not re.search(src["title_must_match"], title, re.I):
        return None, "place"

    where = place(job)
    if not where and src.get("assume_uk"):
        # A UK-only employer whose feed gives an office name, not a town.
        where = {"where": "UK", "label": job["location"] or "United Kingdom"}
    if not where:
        return None, "place"

    if SENIOR_TITLE.search(title):
        return None, "senior title"

    # Which titles count depends on the kind of employer (see sources.json):
    #   ai-policy - an AI governance or policy organisation: any research,
    #               analysis, programme or generalist role.
    #   ai        - an AI company: economics and policy titles, plus a short
    #               list of market-analysis titles.
    #   general   - everyone else: as ai-policy, and the advert must be about AI.
    kind_of_employer = src.get("focus", "general")
    core = bool(CORE_ROLE.search(title)) and not NEVER.search(title)
    if kind_of_employer == "ai":
        suitable = core or (COMPANY_ROLE.search(title) and not OFF_TOPIC.search(title))
    else:
        suitable = core or (BROAD_ROLE.search(title) and not OFF_TOPIC.search(title))
    if not suitable:
        return None, "role"

    if job.get("lazy") and not job["description"]:
        try:
            job["description"] = job["lazy"](job)
        except Exception as err:
            print(f"    could not read advert text for {title!r}: {err}")
    text = job["description"] or ""

    if where["where"] == "Remote" and US_ONLY.search(text):
        return None, "place"

    years = years_required(text)
    if years is not None and years > MAX_YEARS:
        return None, "experience"
    if MANAGER_TITLE.search(title) and not JUNIOR_TITLE.search(title) and years is None:
        return None, "manager title"
    if phd_required(text):
        return None, "PhD required"

    ai_in_title = bool(AI_CASED.search(title) or AI_WORDS.search(title))
    mentions = ai_mentions(text)
    if kind_of_employer in ("ai", "ai-policy"):
        focus = "AI role"       # at an AI organisation every role is about AI
    else:
        need = src.get("min_ai_mentions", 3)
        if ai_in_title:
            focus = "AI role"
        elif src["group"] == "academia":
            # University feeds are full of lab posts that mention AI in passing.
            return None, "not about AI"
        elif mentions >= need and (core or JUNIOR_TITLE.search(title)):
            focus = "AI-related"
        elif src.get("economics") and ECON_ENTRY.search(title) and JUNIOR_TITLE.search(title):
            # Entry-level economics roles at economic consultancies and
            # regulators are listed too, labelled as not specific to AI.
            focus = "Economics"
        else:
            return None, "not about AI"
    if src["group"] == "academia" and not core:
        return None, "role"

    # The deadline: the employer's own feed field first, then the advert text.
    today = TODAY.isoformat()
    feed_closes, text_closes = job.get("closes"), closing_date(text)
    closes = feed_closes or text_closes
    if closes and closes < today:
        return None, "deadline passed"
    posted = job.get("posted")
    if posted and posted > today:
        posted = None

    salary = (job.get("salary") or "").strip()
    if not salary:
        m = SALARY.search(text)
        salary = re.sub(r"\s+", " ", m.group(0)) if m else ""

    employer = job.get("employer") or src["name"]
    record = {
        "id": f"{src['id']}:{job['sid']}",
        "title": re.sub(r"\s+", " ", title),
        "employer": employer,
        "group": job.get("group") or src["group"],
        "via": src["name"] if src.get("aggregator") else "",
        "where": where["where"],
        "location": where["label"],
        "url": job["url"],
        "posted": posted,
        "closes": closes,
        "_closes_feed": feed_closes or "",
        "_closes_text": text_closes or "",
        "salary": salary[:80],
        "kind": kind(title, job.get("employment") or ""),
        "level": job.get("level_hint") or level(title, years),
        "focus": focus,
        "summary": summary(text),
    }
    return record, ""


def collect_source(src: dict) -> dict:
    started = time.time()
    try:
        jobs = ADAPTERS[src["ats"]](src)
    except Exception as err:
        return {"src": src, "ok": False, "error": str(err)[:200], "records": [],
                "seen": 0, "dropped": {}}
    records, dropped, audit = [], {}, []
    for job in jobs:
        record, why = assess(job, src)
        if record:
            records.append(record)
        else:
            dropped[why] = dropped.get(why, 0) + 1
        if why != "place":
            audit.append((src["name"], "KEPT" if record else "dropped", why,
                          job["title"], job["location"], job["url"]))
    return {"src": src, "ok": True, "error": "", "records": records,
            "seen": len(jobs), "dropped": dropped, "audit": audit,
            "seconds": round(time.time() - started, 1)}


def squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def same_employer(a: str, b: str) -> bool:
    a, b = squash(a), squash(b)
    return bool(a and b) and (a in b or b in a)


def place_aggregated(records: list[dict], sources: list[dict]) -> list[dict]:
    """Vacancies from a jobs board that covers many employers: drop the ones an
    employer's own feed already supplied, and give the rest an employer type."""
    direct = [r for r in records if not r.get("via")]
    direct_links = {r["url"].split("?")[0].rstrip("/") for r in direct}
    known = [(s["name"], s["group"]) for s in sources if not s.get("aggregator")]
    out = list(direct)
    for r in records:
        if not r.get("via"):
            continue
        if r["url"].split("?")[0].rstrip("/") in direct_links:
            continue
        if any(same_employer(r["employer"], d["employer"])
               and squash(r["title"]) == squash(d["title"]) for d in direct):
            continue
        # An employer whose own feed is read directly is covered by that feed,
        # with the full advert text to filter on.
        match = next((g for name, g in known if same_employer(r["employer"], name)), None)
        if match and squash(r["employer"]) not in ("ai",):
            continue
        name = r["employer"]
        if re.search(r"universit|college|school of|\bmit\b|\beth\b", name, re.I):
            r["group"] = "academia"
        elif re.search(r"government|department|ministry|commission|parliament|"
                       r"\boffice\b|civil service|council|\bnhs\b|european union|"
                       r"united nations|\boecd\b|security institute", name, re.I):
            r["group"] = "government"
        elif re.search(r"\b(inc|ltd|labs?|technologies|capital|ventures|consulting|"
                       r"partners|group|deepmind|openai|anthropic|microsoft|google|meta|"
                       r"amazon|nvidia|ibm)\b", name, re.I):
            r["group"] = "labs"
        else:
            r["group"] = "thinktank"
        out.append(r)
    return out


def merge_duplicates(records: list[dict]) -> list[dict]:
    """One employer often lists the same role once per office."""
    merged: dict[tuple, dict] = {}
    rank = {"London": 0, "UK": 1, "Remote": 2}
    for r in records:
        key = (r["employer"].lower(), r["title"].lower())
        kept = merged.get(key)
        if not kept:
            merged[key] = r
            continue
        better, other = (r, kept) if rank[r["where"]] < rank[kept["where"]] else (kept, r)
        if other["location"] and other["location"] not in better["location"]:
            better["location"] = f"{better['location']} · {other['location']}"
        better["posted"] = min(filter(None, [better.get("posted"), other.get("posted")]),
                               default=None)
        merged[key] = better
    return list(merged.values())


# -------------------------------------------------------------- link checks --
# Every vacancy that passed the filters has its advert opened, as a student
# pressing the button would. Adverts that have been taken down or have closed
# are removed. The advert page is also read for a posting date and a deadline
# when the employer's feed gave none.

CLOSED_PAGE = rx(
    r"no longer (available|accepting|open|active|advertised|taking|exists?|live)|"
    r"(position|role|vacancy|job|posting|opportunity|opening) (has|is|was) "
    r"(now )?(been )?(filled|closed|removed|expired|withdrawn)|"
    r"applications? (are|have|is|has) (now )?(been )?closed|"
    r"(job|page|posting|vacancy) (was |could |can)?not (be )?found|"
    r"(job|vacancy|posting|advert) (has )?expired|"
    r"page (you (are|were) looking for )?(doesn.t|does not) exist")


def open_page(url: str, timeout: int = 30) -> tuple[int, str, str]:
    """Open an advert. Returns (HTTP status, final address, page source).
    Status 0 means the site could not be reached."""
    hdrs = {"User-Agent": USER_AGENT, "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
    safe = urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")
    status, final, body = 0, url, ""
    for attempt in range(2):
        try:
            req = urllib.request.Request(safe, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (resp.status, resp.geturl(),
                        resp.read(2_000_000).decode("utf-8", "replace"))
        except urllib.error.HTTPError as err:
            status, final = err.code, (err.geturl() or url)
            try:
                body = err.read(200_000).decode("utf-8", "replace")
            except Exception:
                body = ""
            if err.code in (401, 403, 404, 410):
                break
        except Exception:
            status = 0
        time.sleep(3)
    return status, final, body


def moved_away(url: str, final: str) -> bool:
    """True when the advert's address redirects to a general careers page,
    which is what most sites do once a vacancy is taken down."""
    if final.split("#")[0].rstrip("/") == url.split("#")[0].rstrip("/"):
        return False
    a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(final)
    if re.search(r"(^|&)(error|not_?found|expired)=", b.query, re.I):
        return True
    last = a.path.rstrip("/").rsplit("/", 1)[-1].lower()
    if last and last in final.lower():
        return False
    return len(b.path.strip("/").split("/")) < len(a.path.strip("/").split("/"))


def job_posting_meta(doc: str) -> tuple[str | None, str | None]:
    """(datePosted, validThrough) from the JobPosting data that many advert
    pages embed for search engines."""
    for block in re.findall(r"(?is)<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>"
                            r"(.*?)</script>", doc):
        try:
            data = json.loads(block.strip(), strict=False)
        except Exception:
            try:
                data = json.loads(html.unescape(block.strip()), strict=False)
            except Exception:
                continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                kind_ = node.get("@type")
                if kind_ == "JobPosting" or (isinstance(kind_, list) and "JobPosting" in kind_):
                    return iso_date(node.get("datePosted")), iso_date(node.get("validThrough"))
                stack.extend(v for v in node.values() if isinstance(v, (list, dict)))
    return None, None


def check_link(r: dict) -> dict:
    out = {"verdict": "ok", "detail": "", "page_posted": "", "valid_through": "",
           "page_closes": ""}
    try:
        status, final, doc = open_page(r["url"])
    except Exception as err:
        out.update(verdict="unchecked", detail=f"could not open: {str(err)[:80]}")
        return out
    out["detail"] = f"HTTP {status}"
    if status in (404, 410):
        out.update(verdict="removed", detail=f"advert page answers {status}")
        return out
    if status == 0 or status >= 400:
        out.update(verdict="unchecked",
                   detail=f"site answered {status}" if status else "site did not answer")
        return out
    if moved_away(r["url"], final):
        out.update(verdict="removed", detail=f"redirects to {final[:140]}")
        return out
    visible = to_text(doc)
    m = CLOSED_PAGE.search(visible)
    if m:
        snippet = re.sub(r"\s+", " ", visible[max(0, m.start() - 40): m.end() + 40])
        out.update(verdict="removed", detail=f"advert page says: …{snippet}…")
        return out
    posted, through = job_posting_meta(doc)
    out["page_posted"], out["valid_through"] = posted or "", through or ""
    if not r.get("closes"):
        out["page_closes"] = closing_date(page_text_after_heading(doc)[1]) or ""
    return out


def verify(records: list[dict], old_jobs: dict) -> list[dict]:
    """Open every advert, drop the ones that are gone or closed, fill in
    missing dates from the advert page, and write links.tsv."""
    today = TODAY.isoformat()
    with ThreadPoolExecutor(max_workers=8) as pool:
        checks = list(pool.map(check_link, records))
    for r, c in zip(records, checks):
        if c["verdict"] == "ok":
            if not r.get("posted") and c["page_posted"] and c["page_posted"] <= today:
                r["posted"] = c["page_posted"]
            if not r.get("closes") and c["page_closes"]:
                r["closes"] = c["page_closes"]
        if r.get("closes") and r["closes"] < today and c["verdict"] != "removed":
            c.update(verdict="removed", detail=f"deadline passed on {r['closes']}")
        if c["verdict"] == "ok":
            r["checked"] = today
        else:
            r["checked"] = (old_jobs.get(r["id"]) or {}).get("checked")

    removed = [c for c in checks if c["verdict"] == "removed"]
    if len(records) >= 10 and len(removed) > len(records) / 2:
        # More than half failing at once points to a fault in the check itself.
        print(f"\nLink check wanted to remove {len(removed)} of {len(records)} vacancies; "
              "that is implausible, so nothing was removed today.")
        for c in removed:
            c["verdict"] = "unchecked"

    with open(ROOT / "links.tsv", "w", encoding="utf-8") as fh:
        fh.write("verdict\tdetail\temployer\ttitle\tposted\tposted_on_page\tcloses\t"
                 "closes_feed\tcloses_text\tcloses_on_page\tvalid_through\turl\n")
        for r, c in zip(records, checks):
            row = (c["verdict"], c["detail"], r["employer"], r["title"], r.get("posted") or "",
                   c["page_posted"], r.get("closes") or "", r.get("_closes_feed", ""),
                   r.get("_closes_text", ""), c["page_closes"], c["valid_through"], r["url"])
            fh.write("\t".join(str(x).replace("\t", " ").replace("\n", " ") for x in row) + "\n")

    count = {v: sum(1 for c in checks if c["verdict"] == v)
             for v in ("ok", "unchecked", "removed")}
    print(f"\nLink check: {count['ok']} adverts open, {count['unchecked']} could not be "
          f"checked, {count['removed']} removed")
    for r, c in zip(records, checks):
        if c["verdict"] != "ok":
            print(f"  {c['verdict']:<9} {r['employer']}: {r['title'][:60]}  [{c['detail'][:160]}]")
    return [r for r, c in zip(records, checks) if c["verdict"] != "removed"]


def main() -> int:
    config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = [s for s in config["sources"] if not s.get("disabled")]
    for s in sources:
        s.setdefault("id", re.sub(r"[^a-z0-9]+", "-", s["name"].lower()).strip("-"))

    previous = {}
    if OUT_FILE.exists():
        try:
            previous = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    old_jobs = {j["id"]: j for j in previous.get("jobs", [])}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(collect_source, sources))

    # audit.tsv lists every UK or remote advert and why it was kept or dropped.
    with open(ROOT / "audit.tsv", "w", encoding="utf-8") as fh:
        fh.write("employer\tdecision\treason\ttitle\tlocation\turl\n")
        for res in results:
            for row in res.get("audit", []):
                fh.write("\t".join(str(x).replace("\t", " ") for x in row) + "\n")

    records, status = [], []
    for res in results:
        src = res["src"]
        if res["ok"]:
            records.extend(res["records"])
            print(f"  ok   {src['name']:<34} {res['seen']:>5} adverts -> "
                  f"{len(res['records']):>3} kept   {res['dropped']}")
        else:
            # Keep yesterday's vacancies for a source that failed today.
            kept = [j for j in old_jobs.values() if j["id"].startswith(src["id"] + ":")]
            records.extend(kept)
            print(f"  FAIL {src['name']:<34} {res['error']}  (kept {len(kept)} from last run)")
        status.append({"name": src["name"], "group": src["group"], "ok": res["ok"],
                       "careers": src.get("careers", ""), "listed": res["seen"],
                       "kept": len(res["records"])})

    records = merge_duplicates(place_aggregated(records, sources))
    records = verify(records, old_jobs)
    today = TODAY.isoformat()
    for r in records:
        old = old_jobs.get(r["id"]) or {}
        # `posted` stays empty when neither the feed nor the advert gives a
        # date; the page then shows the day the vacancy first appeared here.
        r["first_seen"] = old.get("first_seen") or today
        for key in [k for k in r if k.startswith("_")]:
            del r[key]
    records.sort(key=lambda r: (r.get("posted") or r["first_seen"], r["employer"]), reverse=True)

    failed = [s["name"] for s in status if not s["ok"]]
    out = {
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "new_days": NEW_DAYS,
        "max_years": MAX_YEARS,
        "jobs": records,
        "sources": sorted(status, key=lambda s: (s["group"], s["name"].lower())),
    }
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n{len(records)} vacancies from {len(sources) - len(failed)} of "
          f"{len(sources)} sources written to {OUT_FILE.relative_to(ROOT)}")
    if failed:
        print("Sources that failed today: " + ", ".join(failed))
    # Fail the run only if most sources are down, so one broken feed never
    # takes the page offline.
    return 1 if len(failed) > len(sources) / 2 else 0


if __name__ == "__main__":
    sys.exit(main())

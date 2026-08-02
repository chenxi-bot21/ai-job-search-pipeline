"""Graduate / early-career radar for Singapore + Hong Kong.

Why this exists: opening company career pages one by one does not scale, and the
main pipeline searches by *role* keywords (credit risk analyst, data analyst…),
which misses graduate programmes entirely — they are advertised as "Graduate
Programme", "Management Associate", "Analyst Programme" and so on.

How it works — four layers:
  1. ATS APIs      one HTTP call per employer returns their entire job board as
                   JSON (Workday / Greenhouse / Lever / Ashby / SmartRecruiters
                   + a custom adapter for Guotai Junan International).
  2. Job boards    MyCareersFuture (SG — legally mandatory for any EP-sponsorable
                   role, so it is the only place small employers cannot avoid)
                   and JobsDB HK.
  3. Keyword layer graduate-programme vocabulary, not role vocabulary.
  4. Change detect diff against the previous snapshot so each run reports only
                   what is NEW. That is what makes it a radar, not a search.

Usage (from job_screener/):
    py scripts/grad_radar.py                 # full scan, report NEW since last run
    py scripts/grad_radar.py --all           # report everything, not just new
    py scripts/grad_radar.py --no-boards     # ATS only (fast)
    py scripts/grad_radar.py --no-ats        # boards only
    py scripts/grad_radar.py --terms "management associate,graduate programme"

Outputs (in output/):
    grad_radar_state.json         rolling set of every posting key ever seen
    grad_radar_<date>.json        full normalised result set for this run
    grad_radar_<date>.md          human-readable report (NEW first)

Standalone: stdlib + requests only. Does not import jobscreener, so it runs
even when the main package's environment is unhappy.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print("ERROR: pip install requests", file=sys.stderr)
    raise SystemExit(2)

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "data" / "grad_targets.json"
OUTDIR = ROOT / "output"
STATE = OUTDIR / "grad_radar_state.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
TIMEOUT = 25

# --- what counts as a graduate / early-career posting -------------------------
GRAD_TERMS = [
    "graduate programme", "graduate program", "graduate analyst", "graduate associate",
    "graduate trainee", "graduate scheme", "graduate talent", "graduate development",
    "management associate", "management trainee", "campus", "early career",
    "analyst programme", "analyst program", "rotational", "trainee",
    "fresh graduate", "fresh grad", "entry level", "entry-level",
    "校园招聘", "管培", "培训生", "应届",
]
# Board search terms (fewer, broader — boards match on substrings)
BOARD_TERMS = [
    "graduate programme", "graduate analyst", "management associate",
    "management trainee", "graduate trainee", "analyst programme", "trainee",
]
GRAD_RE = re.compile("|".join(re.escape(t) for t in GRAD_TERMS), re.I)

# Location matching is fiddly: Workday reports internal site codes like
# "HK-ONE ES 21/F" / "CN-Shenzhen-HyQ" / "UK-London", while other ATSs and the
# boards use plain names. Match both shapes, and treat "N Locations" as unknown
# (keep it rather than silently dropping a possible SG/HK role).
LOC_RE = re.compile(
    r"(singapore|hong\s*kong|\bSGP?\b|\bHKG?\b|[\s,;/(]HK[-\s]|^HK[-\s]|[\s,;/(]SG[-\s]|^SG[-\s]"
    r"|sheung\s*wan|kwun\s*tong|quarry\s*bay|tsim\s*sha\s*tsui)", re.I)
MULTI_LOC_RE = re.compile(r"\b\d+\s+locations?\b", re.I)


# A graduate posting is FOR graduates, not ABOUT graduates. Universities in
# particular post plenty of "Assistant Manager, Academic (Graduate Programmes)"
# admin roles that match the vocabulary but are senior hires. Any seniority token
# in the title disqualifies. Note "Management Associate" survives this because the
# word is "Management", not "Manager" — hence the word boundaries.
SENIOR_RE = re.compile(
    r"\b(manager|director|head\s+of|vice\s*president|VP|principal|chief|supervisor"
    r"|managing|professor|lecturer|dean|partner)\b", re.I)


def is_grad(title: str, extra: str = "") -> bool:
    t = f"{title} {extra}"
    return bool(GRAD_RE.search(t)) and not SENIOR_RE.search(title or "")


# --- relevance: which of Chenxi's tracks does this posting belong to? ---------
# A graduate radar without this is unusable: "management trainee" on JobsDB HK
# returns retail, F&B, insurance-agent and HR schemes by the hundred.
TRACKS = {
    "finance/risk": [
        "financ", "risk", "credit", "banking", "bank ", "investment", "treasury",
        "actuar", "audit", "compliance", "aml", "kyc", "capital market", "wealth",
        "asset management", "insurance", "quantitative", "quant", "trading",
        "securities", "fund", "portfolio", "underwrit", "economics", "accounting",
    ],
    "data": [
        "data", "analytic", "analyst", "business intelligence", "machine learning",
        " ai ", "artificial intelligence", "statistic", "modelling", "modeling",
    ],
    "health/biostat": [
        "health", "clinical", "epidemiolog", "biostat", "medical", "pharma",
        "life science", "research assistant", "research associate",
    ],
}
TRACK_RE = {k: re.compile("|".join(re.escape(w) for w in v), re.I) for k, v in TRACKS.items()}

# Hard noise — only applied when NO track matched, so "Sales & Trading" survives.
NOISE_RE = re.compile(
    r"(human\s*resource|\bHR\b|recruit(ment|er)|talent acquisition|retail|store\b"
    r"|supply chain|logistics|warehouse|f\s*&\s*b|hospitality|culinary|chef"
    r"|nursing|nurse|teacher|teaching|mechanical|civil engineer|electrical"
    r"|property agent|beauty|fashion|merchandis|customer service"
    r"|call cent|telemarket|driver|technician|construction)", re.I)

# Two traps that DO match the finance/data vocabulary and so slip past NOISE_RE.
# Both are logged in WORKLOG as real time-wasters, so they get their own gate:
#   1. "Analyst Programmer" is the HK/SEA term for a software developer — it is
#      not an analyst role at all, it just collides with "analyst" + "program".
#   2. Insurance agency roles dressed as management trainees. Commission sales,
#      not corporate finance. Tell-tale: advisor / planner / consultant titles.
TRAP_RE = re.compile(
    r"(analyst\s+programm|programmer|software\s+(dev|engineer)"
    r"|financial\s+(advis|planner|consultant)|business\s+consultant"
    r"|insurance\s+(agent|broker|consultant)|wealth\s+(planner|consultant)"
    r"|agency\s+(leader|manager)|financial\s+planning\s+associate)", re.I)


def classify(title: str, company: str = "") -> tuple[list[str], str]:
    """Return (matched tracks, verdict) where verdict is relevant | other | noise."""
    t = f" {title} {company} "
    if TRAP_RE.search(title or ""):
        return [], "noise"
    hits = [k for k, rx in TRACK_RE.items() if rx.search(t)]
    if hits:
        return hits, "relevant"
    if NOISE_RE.search(t):
        return [], "noise"
    return [], "other"


def is_sg_hk(location: str) -> bool:
    loc = location or ""
    if not loc.strip():
        return True                      # unknown location -> don't drop, let her judge
    if LOC_RE.search(loc):
        return True
    # Bare "N Locations" with no resolved hint stays in (genuinely unknown); once
    # ats_workday has appended the city from the URL, judge on that instead.
    return bool(MULTI_LOC_RE.search(loc)) and "(" not in loc


def rec(source, company, title, location, url, posted="", jid=""):
    """Normalised posting record. `key` is what change-detection dedups on."""
    return {
        "source": source, "company": company, "title": (title or "").strip(),
        "location": (location or "").strip(), "url": url, "posted": posted,
        "id": str(jid),
        "key": f"{company.lower().strip()}|{re.sub(r'[^a-z0-9]+', '', (title or '').lower())}",
    }


# =============================================================================
# ATS adapters — each returns a list of normalised records
# =============================================================================
def ats_workday(t, sess):
    """Workday CXS. POST returns paginated JSON; jobs carry externalPath + locationsText.

    Two tenant quirks, both learned the hard way:
      * `limit` is capped at 20 — asking for 100 returns an EMPTY list, not an error.
      * `total` is only populated on the FIRST page; later pages report total=0, so
        the page count must be captured once up front or pagination dies at page 2.
    """
    out, offset, total, PAGE = [], 0, None, 20
    url = f"https://{t['host']}/wday/cxs/{t['tenant']}/{t['site']}/jobs"
    while offset < 600:                                   # runaway guard
        r = sess.post(url, json={"appliedFacets": {}, "limit": PAGE, "offset": offset,
                                 "searchText": ""},
                      headers={"Accept": "application/json", "Content-Type": "application/json"},
                      timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        posts = data.get("jobPostings") or []
        if total is None:
            total = data.get("total") or 0                # first page only
        if not posts:
            break
        for p in posts:
            path = p.get("externalPath", "")
            loc = p.get("locationsText", "")
            # "N Locations" hides the actual city; the URL path carries it, e.g.
            # /job/Taichung---Fab-16-Taiwan/... — fold it in so the filter can see it.
            if MULTI_LOC_RE.search(loc):
                seg = path.split("/job/")[-1].split("/")[0] if "/job/" in path else ""
                loc = f"{loc} ({seg.replace('-', ' ').strip()})" if seg else loc
            out.append(rec("workday", t["company"], p.get("title", ""), loc,
                           f"https://{t['host']}/{t['site']}{path}",
                           p.get("postedOn", ""), (p.get("bulletFields") or [""])[0]))
        offset += PAGE
        if len(posts) < PAGE or offset >= total:
            break
        time.sleep(0.15)
    return out


def ats_greenhouse(t, sess):
    r = sess.get(f"https://boards-api.greenhouse.io/v1/boards/{t['token']}/jobs",
                 timeout=TIMEOUT)
    r.raise_for_status()
    return [rec("greenhouse", t["company"], j.get("title", ""),
                (j.get("location") or {}).get("name", ""), j.get("absolute_url", ""),
                j.get("updated_at", ""), j.get("id", ""))
            for j in r.json().get("jobs", [])]


def ats_lever(t, sess):
    r = sess.get(f"https://api.lever.co/v0/postings/{t['token']}?mode=json", timeout=TIMEOUT)
    r.raise_for_status()
    return [rec("lever", t["company"], j.get("text", ""),
                (j.get("categories") or {}).get("location", ""), j.get("hostedUrl", ""),
                "", j.get("id", ""))
            for j in r.json()]


def ats_ashby(t, sess):
    r = sess.get(f"https://api.ashbyhq.com/posting-api/job-board/{t['token']}", timeout=TIMEOUT)
    r.raise_for_status()
    return [rec("ashby", t["company"], j.get("title", ""), j.get("location", ""),
                j.get("jobUrl", ""), j.get("publishedAt", ""), j.get("id", ""))
            for j in r.json().get("jobs", [])]


def ats_smartrecruiters(t, sess):
    r = sess.get(f"https://api.smartrecruiters.com/v1/companies/{t['token']}/postings?limit=100",
                 timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        out.append(rec("smartrecruiters", t["company"], j.get("name", ""),
                       f"{loc.get('city','')} {loc.get('country','')}".strip(),
                       f"https://jobs.smartrecruiters.com/{t['token']}/{j.get('id','')}",
                       j.get("releasedDate", ""), j.get("id", "")))
    return out


def ats_gtjai(t, sess):
    """Guotai Junan International runs its own API; every job applies by email."""
    r = sess.post("https://www.gtjai.com/en/api",
                  data={"action": "careerlist", "module": "career", "limit": 200,
                        "page": 1, "lang": "en"}, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json().get("data") or {}
    rows = body.get("list") or body.get("data") or []
    out = []
    for j in rows if isinstance(rows, list) else []:
        jid = j.get("id") or j.get("career_id") or ""
        out.append(rec("gtjai", t["company"], j.get("title") or j.get("name", ""),
                       j.get("location", "Hong Kong"),
                       f"https://www.gtjai.com/en/career/careerdetail/{jid}",
                       j.get("date") or j.get("start_date", ""), jid))
    return out


ATS = {"workday": ats_workday, "greenhouse": ats_greenhouse, "lever": ats_lever,
       "ashby": ats_ashby, "smartrecruiters": ats_smartrecruiters}


# =============================================================================
# Board adapters
# =============================================================================
def board_mcf(term, sess):
    """MyCareersFuture (SG). Mandatory ad board for EP-sponsorable roles."""
    r = sess.get("https://api.mycareersfuture.gov.sg/v2/jobs",
                 params={"search": term, "limit": 100},
                 headers={"Accept": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("results", []):
        pc = j.get("postedCompany") or j.get("hiringCompany") or {}
        out.append(rec("mcf", pc.get("name", "?"), j.get("title", ""), "Singapore",
                       f"https://www.mycareersfuture.gov.sg/job/{j.get('uuid','')}",
                       j.get("originalPostingDate", ""), j.get("uuid", "")))
    return out


def board_jobsdb(term, sess):
    """JobsDB HK. Needs browser-like headers; the v5 search API returns clean JSON."""
    r = sess.get("https://hk.jobsdb.com/api/jobsearch/v5/search",
                 params={"siteKey": "HK-Main", "sourcesystem": "houston",
                         "keywords": term, "page": 1, "pageSize": 100, "locale": "en-HK"},
                 headers={"Accept": "application/json",
                          "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
                          "Sec-Fetch-Site": "same-origin"}, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json().get("data", []):
        adv = (j.get("advertiser") or {}).get("description", "?")
        loc = ", ".join(x.get("label", "") for x in (j.get("locations") or []))
        out.append(rec("jobsdb", adv, j.get("title", ""), loc or "Hong Kong",
                       f"https://hk.jobsdb.com/job/{j.get('id','')}",
                       j.get("listingDate", ""), j.get("id", "")))
    return out


# =============================================================================
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--all", action="store_true", help="report everything, not just new")
    p.add_argument("--no-ats", action="store_true")
    p.add_argument("--no-boards", action="store_true")
    p.add_argument("--terms", help="comma-separated board search terms (overrides default)")
    p.add_argument("--no-filter", action="store_true",
                   help="skip the graduate-keyword filter (dump everything found)")
    p.add_argument("--keep-noise", action="store_true",
                   help="keep off-track schemes (HR / retail / F&B / agent roles)")
    args = p.parse_args(argv)

    OUTDIR.mkdir(exist_ok=True)
    reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

    found, errors = [], []

    # ---- layer 1: ATS -------------------------------------------------------
    if not args.no_ats:
        jobs = [(k, t) for k in ATS for t in reg.get(k, [])] + \
               [("custom", t) for t in reg.get("custom", [])]
        for kind, t in jobs:
            fn = ats_gtjai if kind == "custom" else ATS[kind]
            label = f"{t['company']:<28} [{kind}]"
            try:
                got = fn(t, sess)
                found += got
                print(f"  {label} -> {len(got)}")
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:70]}"
                errors.append((t["company"], kind, msg))
                print(f"  {label} -> ERR {msg}")
            time.sleep(0.3)

    # ---- layer 2: boards ----------------------------------------------------
    if not args.no_boards:
        terms = [t.strip() for t in args.terms.split(",")] if args.terms else BOARD_TERMS
        boards = reg.get("boards", {})
        for term in terms:
            for name, fn in (("mycareersfuture", board_mcf), ("jobsdb_hk", board_jobsdb)):
                if not boards.get(name, {}).get("enabled", True):
                    continue
                try:
                    got = fn(term, sess)
                    found += got
                    print(f"  {name:<16} | {term:<24} -> {len(got)}")
                except Exception as e:
                    msg = f"{type(e).__name__}: {str(e)[:70]}"
                    errors.append((name, term, msg))
                    print(f"  {name:<16} | {term:<24} -> ERR {msg}")
                time.sleep(0.4)

    # ---- layer 3: filter ----------------------------------------------------
    seen_keys, kept = set(), []
    for r in found:
        if r["key"] in seen_keys:
            continue
        seen_keys.add(r["key"])
        if args.no_filter:
            kept.append(r)
            continue
        # board hits are already location-scoped; ATS hits need the location gate
        loc_ok = r["source"] in ("mcf", "jobsdb") or is_sg_hk(r["location"])
        if not (loc_ok and is_grad(r["title"])):
            continue
        r["tracks"], r["verdict"] = classify(r["title"], r["company"])
        if r["verdict"] == "noise" and not args.keep_noise:
            continue
        kept.append(r)

    # ---- layer 4: change detection -----------------------------------------
    prev = set()
    if STATE.exists():
        prev = set(json.loads(STATE.read_text(encoding="utf-8")).get("seen", []))
    new = [r for r in kept if r["key"] not in prev]
    STATE.write_text(json.dumps({"seen": sorted(prev | {r["key"] for r in kept}),
                                 "updated": str(date.today())}, indent=1), encoding="utf-8")

    # ---- output -------------------------------------------------------------
    today = date.today().isoformat()
    (OUTDIR / f"grad_radar_{today}.json").write_text(
        json.dumps({"date": today, "total": len(kept), "new": len(new),
                    "results": kept, "errors": errors}, indent=1, ensure_ascii=False),
        encoding="utf-8")

    show = kept if args.all else new
    n_rel = sum(1 for r in kept if r.get("verdict") == "relevant")
    lines = [f"# Graduate / early-career radar — {today}", "",
             f"Scanned {len(found)} raw postings -> **{len(kept)}** graduate-type in SG/HK "
             f"(**{n_rel}** on-track), of which **{len(new)} are new** since the last run.", ""]
    if not show:
        lines += ["_Nothing new this run._", ""]

    def block(title, rows):
        if not rows:
            return []
        out = [f"## {title} ({len(rows)})", ""]
        for r in sorted(rows, key=lambda x: (x["company"], x["title"])):
            flag = " 🆕" if r["key"] not in prev else ""
            posted = f" · posted {r['posted'][:10]}" if r["posted"] else ""
            tags = f" `{'/'.join(r.get('tracks') or [])}`" if r.get("tracks") else ""
            out.append(f"- **{r['company']}** — {r['title']}{flag}{tags}  \n"
                       f"  {r['location']}{posted} · [{r['source']}]({r['url']})")
        return out + [""]

    lines += block("⭐ On-track (finance / risk / data / health)",
                   [r for r in show if r.get("verdict") == "relevant"])
    lines += block("Unclassified — worth a glance",
                   [r for r in show if r.get("verdict") == "other"])
    if args.keep_noise:
        lines += block("Off-track", [r for r in show if r.get("verdict") == "noise"])
    if errors:
        lines += ["", "## Sources that failed (fix or delete in data/grad_targets.json)", ""]
        lines += [f"- {a} / {b} — {c}" for a, b, c in errors]
    (OUTDIR / f"grad_radar_{today}.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"\n{len(found)} raw -> {len(kept)} grad-type -> {len(new)} NEW")
    print(f"report: output/grad_radar_{today}.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

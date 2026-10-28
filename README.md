# jobscreener — Daily AI Job-Screening Pipeline

<p>
  <a href="https://github.com/chenxi-bot21/ai-job-search-pipeline/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/chenxi-bot21/ai-job-search-pipeline/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="Tests" src="https://img.shields.io/badge/tests-43%20passing-2E4057">
  <img alt="Claude" src="https://img.shields.io/badge/LLM-Claude%20(optional)-D97757">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green">
</p>

An end-to-end job-search assistant that runs the **whole loop**: pull fresh job
postings, **screen** them against your CV with a hard ATS-style *knockout gate*
(work authorization / years of experience / degree) + a transparent fit score,
**curate** the real matches, **track** them in a single Notion tracker, and
**report** what to apply to first.

Two layers: the mechanical steps are a tested Python package (`jobscreener`); the
judgement steps run as a **[Claude Agent Skill](SKILL.md)** (`daily-job-matcher`)
that reads full JDs, curates true fit, writes to Notion, and updates each
application's status from Gmail.

Runs **fully offline with no API key** (bundled sample postings + a transparent
heuristic scorer); transparently upgrades to LLM scoring, a live job source, and
Notion/Gmail sync when configured.

➡️ **The daily cycle: [SKILL.md](SKILL.md) · How the screening decides: [METHODOLOGY.md](METHODOLOGY.md).**

## Workflow → code (mirrors the architecture diagram)

| Workflow node | Module |
|---|---|
| Schedule Trigger (7:30 daily) | OS scheduler → `python -m jobscreener run` |
| Config | `config.py` (profile, rules, weights, paths) |
| RetrieveCV · Extract from File · MapResume | `cv.py` (read .md/.docx/.pdf → skill profile) |
| LinkedIn (job source) | `sources.py` (Apify actor / JobSpy / Remotive / sample; pluggable) |
| NormalizeData | `normalize.py` |
| Filter and deduplicate | `dedup.py` + `filters.py` (ATS-style knockout gate) + `history.py` (cross-run de-dup) |
| Prepare AI Input · Score the job · Parse LLM JSON | `scoring.py` + `llm.py` |
| Merge Scores + Meta · Rank · Top 15 | `pipeline.py` |
| Write to tracker | `notion.py` (Notion) · `report.py` (HTML digest) |
| Send a message (Gmail) | `email_out.py` (optional, SMTP) |

## Quickstart

```bash
pip install -r requirements.txt          # or: pip install -e .
python -m jobscreener run                 # uses bundled sample jobs + your CV
```

By default the CV is read from `../career/PROFILE.md` and jobs from
`data/sample_jobs.json`; an HTML report is written to `output/`.

```
Fetched 26 → 25 after dedup → 18 passed filters → top 15 shown
Dropped: excluded_keyword=3, seniority=2, location=1, stale=1
Scoring: heuristic (no API key)

 1.  84.8  Biostatistician I               Roche             Shanghai, China
 2.  83.6  Graduate Risk Analyst Programme DBS Bank          Singapore
 3.  83.1  Portfolio Risk Analyst          Partners Group    Singapore
 ...
```

### Enable LLM scoring
```bash
export ANTHROPIC_API_KEY=sk-ant-...        # Claude re-scores the shortlist
python -m jobscreener run
```

### Use your own inputs
```bash
python -m jobscreener run --cv path/to/CV.docx --jobs path/to/jobs.json --top 20
python -m jobscreener run --source remotive --search "credit risk"   # live, free API
```

### Live LinkedIn jobs via Apify
[Apify](https://apify.com) rents LinkedIn-jobs scraper *actors* — set a token and
actor id, and the connector runs the actor and ingests the results. **Claude is
not required**: Apify is only the job *source*; scoring still defaults to the
free heuristic (add `ANTHROPIC_API_KEY` to upgrade to LLM re-scoring).
```bash
export APIFY_TOKEN=apify_api_...
export JOBSCREENER_APIFY_ACTOR=johnvc~linkedin-jobs-api
echo '{"keyword": "credit risk", "location": "Hong Kong", "timeRange": "past-week", "maxJobs": 25}' > li_input.json
python -m jobscreener run --source apify --apify-input li_input.json
```

### Live jobs via JobSpy (free, no API key)
[JobSpy](https://github.com/speedyapply/JobSpy) scrapes LinkedIn/Indeed/Glassdoor
into one result set — **no API key, no LLM required**. Two ways:

```bash
pip install python-jobspy
# On Python 3.14, force wheels so pip doesn't try to compile numpy from source:
#   pip install --only-binary=:all: python-jobspy
# (JobSpy pins pandas<3, so this may downgrade pandas — harmless here.)

# A) let this tool scrape + rank in one go
python -m jobscreener run --source jobspy --search "credit risk analyst" \
    --location "Hong Kong" --country "Hong Kong" --sites linkedin,indeed,glassdoor

# B) scrape yourself → save CSV → this tool only ranks it (decoupled, no scraping here)
python -c "from jobspy import scrape_jobs; scrape_jobs(site_name=['indeed'], \
search_term='credit risk analyst', location='Hong Kong', results_wanted=100, \
country_indeed='Hong Kong').to_csv('jobs.csv', index=False)"
python -m jobscreener run --jobs jobs.csv          # auto-detects a JobSpy CSV
```

**Proxies (only if LinkedIn rate-limits you).** Indeed rarely blocks, so start
without proxies; add them only for heavy LinkedIn scraping:
```bash
python -m jobscreener run --source jobspy --proxies "user:pass@1.2.3.4:8000,user:pass@5.6.7.8:8000"
# or set JOBSCREENER_PROXIES in .env
```
A cheap/free proxy pool (e.g. Webshare's free tier) is plenty; the format is
`host:port` or `user:pass@host:port`.

## Scheduling (the "7:30 every morning" trigger)

**Windows (Task Scheduler):**
```powershell
schtasks /create /tn "JobScreener" /tr "python -m jobscreener run" /sc daily /st 07:30
```
**macOS/Linux (cron):** `crontab -e` →
```
30 7 * * *  cd /path/to/job_screener && python -m jobscreener run --email
```

## Notion output (recommended)
Push each day's shortlist to a Notion database (auto when both env vars are set):
1. Create an integration at <https://www.notion.so/my-integrations> → copy its token.
2. Make a database with columns: **Name** (title), **Company**, **Location**,
   **Score** (number), **Status** (select), **Skills**, **URL** (url), **Date** (date), **Source**.
3. Open the database → **⋯ → Connections →** add your integration; copy the
   database id from its URL (the 32-char string).
4. Put `NOTION_TOKEN` and `NOTION_DATABASE_ID` in `.env`.

The writer adapts to whatever columns exist (matched case-insensitively), so a
partial table still works. Skip a run's push with `--no-notion`.

## Email delivery (optional)
Set SMTP env vars (Gmail needs an App Password), then add `--email`:
```
JOBSCREENER_SMTP_HOST=smtp.gmail.com
JOBSCREENER_SMTP_PORT=465
JOBSCREENER_SMTP_USER=you@gmail.com
JOBSCREENER_SMTP_PASS=your_app_password
JOBSCREENER_EMAIL_TO=you@gmail.com
```

## A note on LinkedIn
This project ships **no in-house scraper** (LinkedIn's ToS and anti-bot defences
make DIY scraping brittle and risky). Sources are pluggable behind the
`JobSource` interface in `sources.py`:
- **Apify** (`--source apify`) — rent a managed LinkedIn-jobs actor; you own the
  token and the usage decision.
- **Remotive** (`--source remotive`) — free, open remote-jobs API.
- **Job-board / ATS APIs** (Adzuna, Greenhouse) or a JSON export matching
  `data/sample_jobs.json`.

## Knockout gate (hard requirements)
Before any scoring, `filters.py` applies **non-negotiable knock-outs** — the
things a candidate must meet or be instantly disqualified (see
[METHODOLOGY.md](METHODOLOGY.md)):
- **Eligibility** — citizenship-only / no-visa-sponsorship / clearance roles.
- **Experience** — JDs whose *minimum* required years exceed `max_years_experience` (default 2, i.e. "minimum 3 years" is knocked out for a new grad).
- **Degree** — PhD/postdoc-required roles ("PhD or equivalent" is respected).

Tune these in `CandidateProfile` (`max_years_experience`, `highest_degree`,
`needs_visa_sponsorship`). `--exclude-seen` additionally skips jobs shown on a
previous day (logged to `output/seen_jobs.json`) so a daily run never repeats.

## Configuration
Everything is in `config.py`: `CandidateProfile` (target roles, skills,
locations, hard constraints), `ScreeningRules` (freshness, excluded/senior/title
keywords), `ScoringWeights`, `top_n`, `min_score`, and paths. Secrets come from
env / `.env`.

## Project structure
```
job_screener/
├── src/jobscreener/
│   ├── config.py      · profile, rules, weights, paths
│   ├── cv.py          · read CV → skill profile
│   ├── sources.py     · job sources (sample file / Remotive)
│   ├── normalize.py   · standardise raw postings
│   ├── dedup.py       · de-duplicate re-posts
│   ├── filters.py     · hard filters (freshness/location/seniority/keywords)
│   ├── scoring.py     · heuristic 0-100 fit score (5 dimensions)
│   ├── llm.py         · optional Claude shortlist re-scoring
│   ├── history.py    · cross-run de-dup (skip jobs seen on earlier days)
│   ├── notion.py     · push the shortlist to a Notion database
│   ├── report.py      · HTML email digest
│   ├── email_out.py   · optional SMTP delivery
│   └── pipeline.py    · orchestration
├── data/sample_jobs.json
├── tests/             · 43 unit tests
└── output/            · generated HTML reports
```

## Testing
```bash
python -m unittest discover -s tests -t .
```

## License
MIT © Chenxi Zhao

# AI Sales Agent — Prospect Research & Qualification

An AI-assisted prospect research tool that takes a product profile and an
ICP definition, discovers real candidate companies, researches each one
against live web evidence, and produces an evidence-backed research memo —
not a raw contact list. Every claim in a memo is either cited to a specific
retrieved source or explicitly tagged as inference, so a salesperson can
trust the reasoning instead of guessing which parts are made up.

Phase 1 is complete; Phase 2 is in progress (see **Roadmap** below). It is part of a larger planned system that will
eventually connect to an existing AI WhatsApp sales/RM agent for real
estate leads, carrying one unified conversation context from first research
through outreach, qualification, and demo booking.

## Status

**Phase 1 is functionally complete and validated against live data**
(Exa, Tavily, OpenAI) across several real batches of real estate
developers/brokerages in India. It has an offline test suite (no API keys
or network needed), ruff + mypy configuration, idempotent evidence
retrieval (a rerun after a partial failure only re-queries the missing
signal categories and never duplicates evidence), and up-front validation
of `product_profile.json` that lists every problem at once.

**Phase 2, Milestone 1 (done):** multiple named profiles stored in the
database, with ICP scores (including a per-criterion breakdown), evidence and
memos kept per profile so profiles never overwrite each other's results.
Every pipeline run is recorded with a snapshot of the config it used. The
database schema upgrades itself through versioned migrations.

**Phase 2, Milestone 2 (done):** a local web UI for creating, editing,
duplicating, importing and exporting profiles, and for starting runs and
watching their progress live.

**Phase 2, Milestone 3 (done):** memo review in the UI, replacing the CSV as
the main way to review. Filter and sort a profile's memos; read each one with
clickable citations to its evidence, the ICP score breakdown and any
citation problems; approve or reject with notes (moving on to the next memo
automatically); regenerate a memo from the same evidence; download the CSV.

**Phase 2, Milestone 4 (done):** people and outreach emails. On an approved
memo, "Find people" runs one Exa people search for the profile's target
roles and saves up to 5 people who *currently* work at the company in those
roles (name, title, location, profile link — no email; add it by hand).
"Write email" drafts a short email to one person, grounded only in the
evidence the approved memo cited: every sentence is tagged ([E12] for a fact,
[PRODUCT], [INFERENCE]), checked by the same citation checker as memos, then
stripped for the version you send. The greeting, the profile's
call-to-action link (with a per-email reference code) and the signature are
added by code, not the model. You edit, approve, copy or open it in your
own email app, send it yourself, and mark it sent — nothing is sent
automatically. Contacts are personal data (India's DPDP Act): only work
details are stored, with their source, and deleting a person deletes the
emails written to them.

**UI redesign + cost tracking (done):** the UI is organised around your
work, not the system's parts, in plain words (a profile is a **campaign**, a
run is **Find leads**, a memo is the company's **research**, and signals
read Clear / Likely / Possible need):

- **Home** — your pipeline (found → good fit → to review → approved →
  emails ready → sent), a "what to do next" list, **Find leads** with a live
  cost estimate, and what you've spent this month.
- **Leads** — every company in one list by stage, with its fit, buying
  signal and next step; "not a fit" companies say why.
- **Company page** — a progress bar (research → approve → contact → email →
  sent) and one obvious next action; research with numbered footnotes to its
  sources; "Update research" runs searches that never ran for that company;
  keyboard shortcuts (A approve, R not a fit, J/K next/previous).
- **Campaign setup** in three steps, with the technical settings under
  "Advanced".
- **Real costs**: every paid API call is recorded (Exa's reported cost,
  Tavily credits, OpenAI tokens) against its search or action, so each
  search shows what it actually cost.

## How it works

```
profile (product + ICP, stored in the database; JSON import/export)
        |
        v
[1] Discovery -- one Exa company search per geography finds candidate
    companies and returns their firmographics (employee count, HQ,
    description) in the same call (deterministic, no LLM)
        |
        v
[2] ICP Fit scoring -- weighted rule function over employee count,
    location, industry match (deterministic, no LLM, no API call)
        |
        v
[3] Evidence retrieval -- Tavily runs one bounded search per signal
    category (expansion/launch, hiring, funding, direct pain-point,
    tech adoption) for each company that clears the ICP threshold.
    Pain-point searches go to consumer complaint/review sites, and a
    result only counts if it names the company and complains about
    responsiveness (no reply, no update, no follow-up...)
    (deterministic, no LLM)
        |
        v
[4] Memo generation -- the ONLY LLM step. OpenAI synthesizes the
    product profile + company data + retrieved evidence into a memo.
    Every sentence must cite a specific evidence ID or be tagged
    [INFERENCE] / [NO_EVIDENCE]; a deterministic validator checks this
    after generation, not just the prompt
        |
        v
[5] Signal Confidence -- rule-based (not LLM), derived from the
    evidence the memo actually cited: a direct pain-point complaint ->
    Strong; one of the profile's "likely need" phrases (e.g. hiring
    telecallers, click-to-WhatsApp ads) -> Likely need; other signals
    (launches, hiring, funding, tech) -> Plausible; nothing citable ->
    Weak/unclear. Each memo shows why it got its level. Weak-fit
    companies are surfaced with the tag, never silently dropped.
        |
        v
[6] CSV export, sorted by ICP Fit then Signal Confidence, for human
    review -- no review UI in Phase 1 by design
```

Only step [4] uses an LLM. Everything else is deterministic on purpose —
finding and scoring companies doesn't require judgment, only synthesizing
scattered evidence into a coherent argument does.

## Setup

```
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env` with your own keys:

| Key | Used for | Notes |
|---|---|---|
| `OPENAI_API_KEY` / `OPENAI_MODEL` | Memo generation | Defaults to `gpt-6-luna` (cost-efficient tier). `gpt-6-astra` (flagship) is available if quality needs outweigh cost. |
| `EXA_API_KEY` | Discovery + company firmographics | Free tier: 20,000 requests/month. Chosen over Tavily for discovery because Exa's company search benchmarks meaningfully better on this task, and its `category="company"` results include structured company data — which replaced a separate Apollo enrichment step whose free-tier credits ran out. |
| `TAVILY_API_KEY` | Evidence retrieval | Free tier: 1,000 credits/month. Stays on Tavily — better fit for research-style per-signal queries than for company discovery. |

## Running it

### Web UI

```
.\.venv\Scripts\python.exe run.py serve
```

Then open http://127.0.0.1:8000 — Home tells you what to do next. The UI
only listens on your own machine (127.0.0.1) and has no login, so it isn't
reachable from other devices. It also refuses form submissions coming from
other websites, so a page you visit can't start a search (and spend API
credits) behind your back. Only one search runs at a time; one interrupted
by stopping the server is marked failed the next time it starts. The pages
use Google Fonts when online and fall back to system fonts offline.

### Command line

Settings live in named **profiles** (product + ICP + signal queries). The
JSON file format is how profiles are imported and exported:

```
# first time: create a profile from the JSON file
.\.venv\Scripts\python.exe run.py import-profile config\product_profile.json --name Default

# run the pipeline for a profile
.\.venv\Scripts\python.exe run.py run --profile Default --limit 25

# other commands
.\.venv\Scripts\python.exe run.py profiles                                   # list profiles
.\.venv\Scripts\python.exe run.py import-profile my.json --name Default --replace  # update one
.\.venv\Scripts\python.exe run.py export-profile Default my.json             # save one to a file
```

`--limit` (in the UI: "New companies to find") caps how many *new*
companies a run looks for: companies the profile has already evaluated are
skipped, and further locations are searched until the limit is reached or
the locations run out. Raise it once you're comfortable with the API cost
(see below). Output lands in `output\memos_<profile>_<timestamp>.csv`.
A rerun also retries any qualifying company that still lacks a memo, and
reuses evidence already fetched.

Each location supplies at most ~25 candidates, so once a profile has seen
them all, runs find nothing new — the run page says so. Add locations or
change the industry keywords to widen the search.

To try a different product, ICP, or geography, create another profile — its
scores and memos are kept separately. Configs are validated before anything
is saved or run; each `signal_taxonomy` template must contain `{company}`.

The database (`data\pipeline.sqlite`) upgrades its own schema on first use
after an update. A database from before profiles existed is adopted into a
profile named "Default", created from `config\product_profile.json`.

## Development

```
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
```

Tests use a throwaway SQLite file and fake the external APIs, so they cost
nothing and never touch `data\pipeline.sqlite`.

## Cost per run

Real costs are recorded as you go: each search's page shows what it cost,
and Home shows this month's total and how many of Tavily's free credits are
used. Prices live in `src/usage.py` and can be overridden in `.env`
(`TAVILY_PRICE_PER_CREDIT`, `TAVILY_FREE_CREDITS_PER_MONTH`,
`OPENAI_PRICE_PER_M_INPUT`, `OPENAI_PRICE_PER_M_OUTPUT`); Exa reports its own
cost per call. Rough figures:

For a batch that finds ~15-20 qualifying companies out of ~30-35
discovered candidates:

- **Exa**: one search per location until enough new companies are found
  (at most one per configured location), ~$0.022 each for 25 results —
  discovery and enrichment together cost at most about 15 cents.
- **Tavily**: 5 searches per qualifying company (one per signal category).
- **OpenAI**: 1 call per qualifying company — about 3,000 tokens in and
  400 out, roughly $0.0005 each on `gpt-6-luna` ($0.10 / $0.50 per million
  tokens, Sept 2026).

Total for a batch of ~20 qualifying companies: roughly **$0.85**, about 90%
of it Tavily (~100 credits at $0.008 on pay-as-you-go; on the free plan that
is 10% of the 1,000 monthly credits).

Outreach, per approved company: one Exa people search (~$0.007) and one
OpenAI call per email (~$0.0005).

Known limits of Exa's company data: it has no industry label (the ICP
scorer matches industry keywords against the description instead), no
state, and the city is occasionally blank — so location matching leans on
the country when "India" is one of the configured geographies.

## Project layout

```
config/product_profile.json   Starter profile (JSON import/export format)
src/
  config.py           Loads + validates profile configs (JSON format)
  db.py               SQLite connection + versioned schema migrations
  profiles.py         Named profiles stored in the database
  runs.py             Pipeline run records (status, stats, config snapshot)
  review.py           Research review: decisions, regenerate, update research
  leads.py            Stages, next steps and the Home dashboard (derived, never stored)
  usage.py            Real API cost recording, monthly totals, run estimates
  contacts.py         People at a company: Exa people search + manual, DPDP-minded
  outreach.py         Grounded email drafts: write, edit, approve, mark sent
  citations.py        Shared citation checker (memos + emails)
  llm.py              Shared OpenAI structured-output call with retries
  web/                Local web UI (FastAPI + Jinja2 templates + htmx)
    app.py            App, campaigns, searches, email drafts + local-only safety checks
    lead_pages.py     Home, Leads and the company page, and actions on a company
    templating.py     Shared Jinja2 environment and display filters
    forms.py          Campaign form <-> config conversion, per-field errors
    runner.py         Background search thread (one at a time)
    rendering.py      Escaped research HTML with footnotes; safe URLs
  discovery.py         Exa company search + firmographics, with noise filters
  exa_client.py        Shared Exa search client
  tavily_client.py      Shared Tavily search client
  icp_scorer.py        Deterministic ICP Fit scoring
  evidence.py          Bounded per-category evidence retrieval (Tavily)
  memo_generator.py     LLM memo generation (OpenAI)
  confidence.py        Rule-based Signal Confidence assignment
  export.py            CSV export (file after each run, or UI download)
  pipeline.py          Orchestrates one run for one profile
  retry.py             Shared retry-with-backoff for all external calls
run.py                 CLI: serve, run, profiles, import-profile, export-profile
tests/                 Offline pytest suite (APIs faked, temp DB)
pyproject.toml         pytest / ruff / mypy configuration
```

## Roadmap

- **Phase 1 (this)**: product + ICP -> discovered, scored, evidence-backed,
  confidence-tagged research memos -> human review via CSV.
- **Phase 2** (in progress): profiles editable in a local web UI (FastAPI +
  htmx), review UI backed by the database instead of CSV, grounded email
  outreach drafts from approved memos with one configurable call-to-action
  link (human still sends). Milestones 1 (profiles in the database,
  per-profile results, run tracking), 2 (web UI for profiles and runs),
  3 (memo review UI) and 4 (people + outreach email drafts) are done.
- **Phase 3**: email-address enrichment; reply classification;
  unified-context integration with the existing WhatsApp/voice sales agent.
- **Phase 4**: automated sending with guardrails, broader signal sources,
  feedback loop from real conversion outcomes back into ICP/confidence
  weighting.

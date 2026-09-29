# AI Sales Agent — Prospect Research & Qualification (Phase 1)

An AI-assisted prospect research tool that takes a product profile and an
ICP definition, discovers real candidate companies, researches each one
against live web evidence, and produces an evidence-backed research memo —
not a raw contact list. Every claim in a memo is either cited to a specific
retrieved source or explicitly tagged as inference, so a salesperson can
trust the reasoning instead of guessing which parts are made up.

This is Phase 1 of a larger planned system (see **Roadmap** below) that will
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

## How it works

```
product_profile.json (product + ICP, human-edited)
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
    tech adoption) for each company that clears the ICP threshold
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
[5] Signal Confidence -- rule-based (not LLM), derived from which
    evidence categories the model actually cited: a direct pain-point
    citation -> Strong, expansion/hiring/etc. -> Plausible, nothing
    citable -> Weak/unclear. Weak-fit companies are surfaced with the
    tag, never silently dropped.
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

```
.\.venv\Scripts\python.exe run.py config\product_profile.json 25
```

The second argument caps how many companies discovery attempts to find —
raise it once you're comfortable with the API cost (see below). Output
lands in `output\memos_<timestamp>.csv`. Delete `data\pipeline.sqlite`
between runs for a clean slate; otherwise a rerun skips any company that
already has a memo.

Edit `config\product_profile.json` directly to test a different product,
ICP, or geography — no code changes needed. The config is validated before
any API call; each `signal_taxonomy` template must contain `{company}`.

## Development

```
.\.venv\Scripts\pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy
```

Tests use a throwaway SQLite file and fake the external APIs, so they cost
nothing and never touch `data\pipeline.sqlite`.

## Cost per run (rough)

For a batch that finds ~15-20 qualifying companies out of ~30-35
discovered candidates:

- **Exa**: ~7 discovery searches (one per configured geography), ~$0.007
  each — discovery and enrichment together cost about 5 cents.
- **Tavily**: 5 searches per qualifying company (one per signal category).
- **OpenAI**: 1 call per qualifying company, ~$0.01-0.04 each on
  `gpt-6-luna` depending on evidence volume.

Total for a real batch: typically **under $1-2**, mostly OpenAI + Tavily.

Known limits of Exa's company data: it has no industry label (the ICP
scorer matches industry keywords against the description instead), no
state, and the city is occasionally blank — so location matching leans on
the country when "India" is one of the configured geographies.

## Project layout

```
config/product_profile.json   Product + ICP definition (human-edited)
src/
  config.py           Loads + validates product_profile.json
  db.py               SQLite schema: companies, evidence_items, memos
  discovery.py         Exa company search + firmographics, with noise filters
  exa_client.py        Shared Exa search client
  tavily_client.py      Shared Tavily search client
  icp_scorer.py        Deterministic ICP Fit scoring
  evidence.py          Bounded per-category evidence retrieval (Tavily)
  memo_generator.py     LLM memo generation + citation validator (OpenAI)
  confidence.py        Rule-based Signal Confidence assignment
  export.py            CSV export
  pipeline.py          Orchestrates the stages above
  retry.py             Shared retry-with-backoff for all external calls
run.py                 CLI entrypoint
tests/                 Offline pytest suite (APIs faked, temp DB)
pyproject.toml         pytest / ruff / mypy configuration
```

## Roadmap

- **Phase 1 (this)**: product + ICP -> discovered, scored, evidence-backed,
  confidence-tagged research memos -> human review via CSV.
- **Phase 2**: real review UI backed by a database instead of CSV; grounded
  outreach-draft generation from approved memos (human still sends).
- **Phase 3**: decision-maker/contact enrichment; reply classification;
  unified-context integration with the existing WhatsApp/voice sales agent.
- **Phase 4**: automated sending with guardrails, broader signal sources,
  feedback loop from real conversion outcomes back into ICP/confidence
  weighting.

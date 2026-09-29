import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pipeline.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    domain TEXT,
    employee_count INTEGER,
    industry TEXT,
    city TEXT,
    state TEXT,
    country TEXT,
    short_description TEXT,
    raw_json TEXT,
    icp_fit_score REAL,
    icp_fit_label TEXT,
    discovered_at TEXT
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL,
    category TEXT NOT NULL,
    fact_text TEXT NOT NULL,
    source_url TEXT,
    source_date TEXT,
    retrieved_at TEXT,
    FOREIGN KEY (company_id) REFERENCES companies (id)
);

-- Makes evidence retrieval safe to re-run: a retried company can't collect
-- duplicate rows, which the memo prompt would otherwise see twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_unique
    ON evidence_items (company_id, category, source_url);

CREATE TABLE IF NOT EXISTS memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id TEXT NOT NULL,
    icp_fit_score REAL,
    icp_fit_label TEXT,
    signal_confidence TEXT,
    why_relevant_text TEXT,
    potential_use_case_text TEXT,
    evidence_ids_json TEXT,
    citation_issues TEXT,
    review_status TEXT DEFAULT 'pending',
    generated_at TEXT,
    FOREIGN KEY (company_id) REFERENCES companies (id)
);
"""


def get_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn

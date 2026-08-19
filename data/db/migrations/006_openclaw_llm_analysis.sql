PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS issue_llm_theses (
  issue_date TEXT PRIMARY KEY REFERENCES issues(issue_date) ON DELETE CASCADE,
  theses_json TEXT NOT NULL DEFAULT '[]',
  brief TEXT,
  provider TEXT NOT NULL DEFAULT 'openclaw',
  model TEXT,
  prompt_version TEXT NOT NULL,
  request_path TEXT,
  response_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_issue_llm_theses_status ON issue_llm_theses(status, issue_date);

CREATE TABLE IF NOT EXISTS material_llm_summaries (
  material_id TEXT PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
  issue_date TEXT NOT NULL,
  short_text TEXT NOT NULL,
  agpm_angle TEXT,
  provider TEXT NOT NULL DEFAULT 'openclaw',
  model TEXT,
  prompt_version TEXT NOT NULL,
  request_path TEXT,
  response_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_material_llm_summaries_issue ON material_llm_summaries(issue_date, status);

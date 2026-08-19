PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS issue_period_theses (
  as_of_issue_date TEXT NOT NULL REFERENCES issues(issue_date) ON DELETE CASCADE,
  period TEXT NOT NULL CHECK(period IN ('7d', '30d')),
  start_issue_date TEXT NOT NULL,
  end_issue_date TEXT NOT NULL,
  issue_count INTEGER NOT NULL DEFAULT 0,
  material_count INTEGER NOT NULL DEFAULT 0,
  stats_json TEXT NOT NULL DEFAULT '{}',
  theses_json TEXT NOT NULL DEFAULT '[]',
  brief TEXT,
  provider TEXT,
  model TEXT,
  prompt_version TEXT NOT NULL,
  request_path TEXT,
  response_path TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (as_of_issue_date, period)
);

CREATE INDEX IF NOT EXISTS idx_issue_period_theses_period ON issue_period_theses(period, as_of_issue_date);

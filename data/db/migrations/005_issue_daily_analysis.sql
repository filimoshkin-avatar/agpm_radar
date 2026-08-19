PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS issue_daily_analysis (
  issue_date TEXT PRIMARY KEY REFERENCES issues(issue_date) ON DELETE CASCADE,
  headline TEXT,
  analysis_json TEXT NOT NULL DEFAULT '{}',
  provider TEXT,
  model TEXT,
  prompt_version TEXT NOT NULL,
  request_path TEXT,
  response_path TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_issue_daily_analysis_status ON issue_daily_analysis(status, issue_date);

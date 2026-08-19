PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS issues (
  issue_date TEXT PRIMARY KEY,
  issue_number INTEGER,
  title TEXT,
  brief TEXT,
  theses_json TEXT NOT NULL DEFAULT '[]',
  report_md_path TEXT,
  report_docx_path TEXT,
  status TEXT NOT NULL DEFAULT 'draft',
  published_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  canonical_url TEXT,
  source_name TEXT,
  source_id TEXT,
  published_at TEXT,
  first_seen_at TEXT,
  radar_issue_date TEXT,
  publication_date_source TEXT,
  publication_date_confidence REAL,
  publication_date_status TEXT NOT NULL DEFAULT 'unresolved',
  perimeter TEXT,
  verdict TEXT,
  summary TEXT,
  agpm_takeaway TEXT,
  governance_flag INTEGER NOT NULL DEFAULT 0,
  security_flag INTEGER NOT NULL DEFAULT 0,
  human_in_the_loop_flag INTEGER NOT NULL DEFAULT 0,
  pmo_flag INTEGER NOT NULL DEFAULT 0,
  isup_flag INTEGER NOT NULL DEFAULT 0,
  mcp_flag INTEGER NOT NULL DEFAULT 0,
  key_material INTEGER NOT NULL DEFAULT 0,
  docx_source_path TEXT,
  md_source_path TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(canonical_url, radar_issue_date)
);

CREATE INDEX IF NOT EXISTS idx_materials_published_at ON materials(published_at);
CREATE INDEX IF NOT EXISTS idx_materials_first_seen_at ON materials(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_materials_issue ON materials(radar_issue_date);
CREATE INDEX IF NOT EXISTS idx_materials_perimeter ON materials(perimeter);
CREATE INDEX IF NOT EXISTS idx_materials_verdict ON materials(verdict);

CREATE TABLE IF NOT EXISTS source_metadata (
  url TEXT PRIMARY KEY,
  canonical_url TEXT,
  title TEXT,
  extracted_published_at TEXT,
  extraction_source TEXT,
  confidence REAL,
  status TEXT NOT NULL DEFAULT 'unresolved',
  fetched_at TEXT,
  http_status INTEGER,
  content_type TEXT,
  snapshot_path TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS rubrics (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS material_rubrics (
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  rubric_id TEXT NOT NULL REFERENCES rubrics(id) ON DELETE CASCADE,
  confidence REAL,
  source TEXT,
  PRIMARY KEY (material_id, rubric_id)
);

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
  stat_date TEXT PRIMARY KEY,
  viewed INTEGER NOT NULL DEFAULT 0,
  included INTEGER NOT NULL DEFAULT 0,
  cut INTEGER NOT NULL DEFAULT 0,
  near INTEGER NOT NULL DEFAULT 0,
  mid INTEGER NOT NULL DEFAULT 0,
  far INTEGER NOT NULL DEFAULT 0,
  core INTEGER NOT NULL DEFAULT 0,
  adjacent INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS llm_classifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
  provider TEXT,
  model TEXT,
  prompt_version TEXT NOT NULL,
  request_path TEXT,
  response_path TEXT,
  normalized_json TEXT,
  confidence REAL,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rejected_materials_internal (
  id TEXT PRIMARY KEY,
  title TEXT,
  url TEXT,
  canonical_url TEXT,
  first_seen_at TEXT,
  radar_issue_date TEXT,
  reason TEXT,
  source_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT,
  details_json TEXT,
  error TEXT
);

INSERT OR IGNORE INTO rubrics (id, title, sort_order) VALUES
  ('agpm_pmo_portfolio', 'AgPM, PMO и портфели', 10),
  ('isup_coordination', 'ИСУП и проектная координация', 20),
  ('governance_control', 'Governance и контроль', 30),
  ('human_responsibility', 'Ответственность человека', 40),
  ('workflow_orchestration', 'Процессы и оркестрация', 50),
  ('security_access', 'Безопасность и доступ', 60),
  ('mcp_gateways_infra', 'Инфраструктура агентов и MCP', 70),
  ('enterprise_adoption', 'Внедрение в enterprise', 80),
  ('vendors_releases', 'Вендоры и продуктовые релизы', 90),
  ('research_methodology', 'Исследования и методология', 100),
  ('funding_ma', 'Инвестиции и сделки', 110);

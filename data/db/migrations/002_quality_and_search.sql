PRAGMA foreign_keys = ON;

ALTER TABLE materials ADD COLUMN brief TEXT;
ALTER TABLE materials ADD COLUMN theses_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE materials ADD COLUMN trend_notes TEXT;

CREATE TABLE IF NOT EXISTS source_domain_rules (
  host TEXT PRIMARY KEY,
  date_strategy TEXT NOT NULL,
  notes TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS material_date_quality (
  material_id TEXT PRIMARY KEY REFERENCES materials(id) ON DELETE CASCADE,
  source_host TEXT,
  publication_date_status TEXT NOT NULL,
  issue_date_delta_days INTEGER,
  severity TEXT NOT NULL DEFAULT 'ok',
  review_status TEXT NOT NULL DEFAULT 'ok',
  reason TEXT,
  diagnostic_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_material_date_quality_review ON material_date_quality(review_status, severity);
CREATE INDEX IF NOT EXISTS idx_material_date_quality_host ON material_date_quality(source_host);

CREATE VIRTUAL TABLE IF NOT EXISTS materials_fts USING fts5(
  material_id UNINDEXED,
  title,
  summary,
  agpm_takeaway,
  source_name,
  url
);

INSERT OR IGNORE INTO source_domain_rules(host, date_strategy, notes) VALUES
  ('youtube.com', 'structured_or_manual', 'Часто нужна проверка uploadDate или страницы видео.'),
  ('youtu.be', 'structured_or_manual', 'Короткие ссылки YouTube нормализуются отдельно.'),
  ('prnewswire.com', 'structured_preferred', 'Обычно дата есть в structured data или visible date.'),
  ('reddit.com', 'low_confidence_manual', 'Публичные страницы могут отдавать нестабильную разметку.'),
  ('github.com', 'structured_or_repository_context', 'Дата страницы не всегда равна дате релиза или материала.'),
  ('openai.com', 'structured_preferred', 'Обычно дата есть в metadata или structured data.');

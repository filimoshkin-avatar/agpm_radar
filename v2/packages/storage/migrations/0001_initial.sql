CREATE TABLE schema_migrations (
  version TEXT PRIMARY KEY,
  checksum TEXT NOT NULL CHECK(length(checksum) = 64 AND checksum NOT GLOB '*[^0-9a-f]*'),
  applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE application_compatibility (
  application_release_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
  table_contract_version TEXT NOT NULL,
  candidate_contract_version TEXT NOT NULL,
  delta_contract_version TEXT NOT NULL,
  result_contract_version TEXT NOT NULL,
  gazette_contract_version TEXT NOT NULL,
  public_api_version TEXT NOT NULL,
  sqlite_runtime_version TEXT NOT NULL,
  activated_at TEXT NOT NULL
) STRICT;

CREATE TABLE content_releases (
  release_id TEXT PRIMARY KEY,
  sequence INTEGER NOT NULL UNIQUE CHECK(sequence >= 0),
  base_release_id TEXT,
  candidate_id TEXT NOT NULL UNIQUE,
  operation TEXT NOT NULL CHECK(operation IN ('daily', 'correction', 'gazette')),
  schema_version INTEGER NOT NULL CHECK(schema_version >= 1),
  before_state_hash TEXT NOT NULL CHECK(length(before_state_hash) = 64 AND before_state_hash NOT GLOB '*[^0-9a-f]*'),
  after_state_hash TEXT NOT NULL CHECK(length(after_state_hash) = 64 AND after_state_hash NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  activated_at TEXT NOT NULL
) STRICT;

CREATE TABLE source_snapshots (
  snapshot_id TEXT PRIMARY KEY,
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256) = 64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  collected_at TEXT NOT NULL,
  item_count INTEGER NOT NULL CHECK(item_count >= 0)
) STRICT;

CREATE TABLE sources (
  source_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT,
  source_type TEXT NOT NULL,
  enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE materials (
  material_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  canonical_url TEXT,
  source_name TEXT,
  published_at TEXT,
  publication_date_status TEXT NOT NULL CHECK(publication_date_status IN ('resolved', 'low_confidence', 'unresolved')),
  summary TEXT,
  agpm_takeaway TEXT,
  brief TEXT,
  content_hash TEXT NOT NULL CHECK(length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE material_sources (
  material_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_url TEXT,
  provider TEXT,
  first_seen_at TEXT,
  last_seen_at TEXT,
  PRIMARY KEY (material_id, source_id),
  FOREIGN KEY (material_id) REFERENCES materials(material_id) ON DELETE CASCADE,
  FOREIGN KEY (source_id) REFERENCES sources(source_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX idx_material_sources_source_id ON material_sources(source_id, material_id);

CREATE TABLE material_evidence (
  evidence_id TEXT PRIMARY KEY,
  material_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  media_type TEXT NOT NULL,
  public_url TEXT,
  metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
  created_at TEXT NOT NULL,
  FOREIGN KEY (material_id) REFERENCES materials(material_id) ON DELETE CASCADE
) STRICT;

CREATE INDEX idx_material_evidence_material_id ON material_evidence(material_id, evidence_id);

CREATE TABLE editorial_queue (
  queue_id TEXT PRIMARY KEY,
  material_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('manual', 'deferred', 'review')),
  target_issue_date TEXT,
  priority INTEGER NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (material_id) REFERENCES materials(material_id) ON DELETE CASCADE
) STRICT;

CREATE INDEX idx_editorial_queue_material_id ON editorial_queue(material_id, queue_id);
CREATE INDEX idx_editorial_queue_state_target ON editorial_queue(state, target_issue_date, priority, queue_id);

CREATE TABLE issues (
  issue_id TEXT PRIMARY KEY,
  issue_date TEXT NOT NULL UNIQUE,
  issue_number INTEGER,
  title TEXT NOT NULL,
  brief TEXT,
  lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('draft', 'published')),
  published_at TEXT,
  publication_origin TEXT CHECK(publication_origin IN ('v2', 'legacy_inferred')),
  empty_reason TEXT,
  content_hash TEXT NOT NULL CHECK(length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK(
    (lifecycle_status = 'draft' AND publication_origin IS NULL AND published_at IS NULL)
    OR (lifecycle_status = 'published' AND publication_origin = 'v2' AND published_at IS NOT NULL)
    OR (lifecycle_status = 'published' AND publication_origin = 'legacy_inferred')
  )
) STRICT;

CREATE INDEX idx_issues_lifecycle_date ON issues(lifecycle_status, issue_date);

CREATE TABLE legacy_issue_provenance (
  issue_id TEXT PRIMARY KEY,
  legacy_status TEXT,
  legacy_published_at TEXT,
  baseline_database_sha256 TEXT NOT NULL CHECK(length(baseline_database_sha256) = 64 AND baseline_database_sha256 NOT GLOB '*[^0-9a-f]*'),
  legacy_issue_row_sha256 TEXT NOT NULL CHECK(length(legacy_issue_row_sha256) = 64 AND legacy_issue_row_sha256 NOT GLOB '*[^0-9a-f]*'),
  imported_at TEXT NOT NULL,
  FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE legacy_publication_evidence (
  issue_id TEXT NOT NULL,
  evidence_kind TEXT NOT NULL,
  relative_path TEXT NOT NULL CHECK(
    relative_path <> '' AND substr(relative_path, 1, 1) <> '/'
    AND instr(relative_path, '\\') = 0
    AND relative_path <> '..' AND relative_path NOT LIKE '../%'
    AND relative_path NOT LIKE '%/../%' AND relative_path NOT LIKE '%/..'
  ),
  sha256 TEXT NOT NULL CHECK(length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
  evidence_status TEXT NOT NULL CHECK(evidence_status IN ('passed', 'failed')),
  details_json TEXT NOT NULL CHECK(json_valid(details_json)),
  PRIMARY KEY (issue_id, evidence_kind),
  FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE RESTRICT
) STRICT;

CREATE TABLE issue_materials (
  issue_id TEXT NOT NULL,
  material_id TEXT NOT NULL,
  sort_order INTEGER NOT NULL CHECK(sort_order >= 0),
  perimeter TEXT NOT NULL CHECK(perimeter IN ('near', 'mid', 'far')),
  verdict TEXT NOT NULL CHECK(verdict IN ('core', 'adjacent')),
  summary TEXT,
  agpm_takeaway TEXT,
  brief TEXT,
  theses_json TEXT NOT NULL CHECK(json_valid(theses_json)),
  trend_notes TEXT,
  flags_json TEXT NOT NULL CHECK(json_valid(flags_json)),
  key_material INTEGER NOT NULL CHECK(key_material IN (0, 1)),
  signal_score INTEGER,
  signal_strength TEXT NOT NULL CHECK(signal_strength IN ('strong', 'context', 'watch')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (issue_id, material_id),
  UNIQUE (issue_id, sort_order),
  FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE CASCADE,
  FOREIGN KEY (material_id) REFERENCES materials(material_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX idx_issue_materials_material_id ON issue_materials(material_id, issue_id);

CREATE TABLE issue_analysis (
  issue_id TEXT PRIMARY KEY,
  headline TEXT,
  analysis_json TEXT NOT NULL CHECK(json_valid(analysis_json)),
  theses_json TEXT NOT NULL CHECK(json_valid(theses_json)),
  brief TEXT,
  llm_status TEXT NOT NULL CHECK(llm_status IN ('success', 'fallback', 'unavailable')),
  requested_model TEXT,
  effective_model TEXT,
  provider TEXT,
  prompt_version TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE material_analysis (
  issue_id TEXT NOT NULL,
  material_id TEXT NOT NULL,
  short_text TEXT,
  agpm_angle TEXT,
  llm_status TEXT NOT NULL CHECK(llm_status IN ('success', 'fallback', 'unavailable')),
  requested_model TEXT,
  effective_model TEXT,
  provider TEXT,
  prompt_version TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (issue_id, material_id),
  FOREIGN KEY (issue_id, material_id) REFERENCES issue_materials(issue_id, material_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE llm_attempts (
  attempt_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL CHECK(scope IN ('issue', 'material')),
  issue_id TEXT NOT NULL,
  material_id TEXT,
  requested_model TEXT,
  attempted_model TEXT,
  provider TEXT,
  attempt_order INTEGER NOT NULL CHECK(attempt_order >= 1),
  status TEXT NOT NULL CHECK(status IN ('success', 'error', 'invalid', 'skipped')),
  error_code TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  CHECK((scope = 'issue' AND material_id IS NULL) OR (scope = 'material' AND material_id IS NOT NULL)),
  FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE CASCADE
) STRICT;

CREATE INDEX idx_llm_attempts_issue ON llm_attempts(issue_id, scope, attempt_order, attempt_id);

CREATE TABLE source_rules (
  host TEXT PRIMARY KEY,
  date_strategy TEXT NOT NULL,
  notes TEXT,
  updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE material_quality (
  issue_id TEXT NOT NULL,
  material_id TEXT NOT NULL,
  publication_date_status TEXT NOT NULL CHECK(publication_date_status IN ('resolved', 'low_confidence', 'unresolved')),
  issue_date_delta_days INTEGER,
  severity TEXT NOT NULL CHECK(severity IN ('ok', 'low', 'medium', 'high')),
  review_status TEXT NOT NULL CHECK(review_status IN ('ok', 'monitor', 'queued')),
  reason TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (issue_id, material_id),
  FOREIGN KEY (issue_id, material_id) REFERENCES issue_materials(issue_id, material_id) ON DELETE CASCADE
) STRICT;

CREATE INDEX idx_material_quality_review ON material_quality(review_status, severity, issue_id, material_id);

CREATE TABLE rubrics (
  rubric_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  sort_order INTEGER NOT NULL
) STRICT;

CREATE TABLE material_rubrics (
  issue_id TEXT NOT NULL,
  material_id TEXT NOT NULL,
  rubric_id TEXT NOT NULL,
  confidence REAL,
  source TEXT NOT NULL,
  PRIMARY KEY (issue_id, material_id, rubric_id),
  FOREIGN KEY (issue_id, material_id) REFERENCES issue_materials(issue_id, material_id) ON DELETE CASCADE,
  FOREIGN KEY (rubric_id) REFERENCES rubrics(rubric_id) ON DELETE RESTRICT
) STRICT;

CREATE INDEX idx_material_rubrics_rubric ON material_rubrics(rubric_id, issue_id, material_id);

CREATE TABLE daily_stats (
  issue_id TEXT PRIMARY KEY,
  viewed INTEGER NOT NULL CHECK(viewed >= 0),
  included INTEGER NOT NULL CHECK(included >= 0),
  cut INTEGER NOT NULL CHECK(cut >= 0),
  near INTEGER NOT NULL CHECK(near >= 0),
  mid INTEGER NOT NULL CHECK(mid >= 0),
  far INTEGER NOT NULL CHECK(far >= 0),
  core INTEGER NOT NULL CHECK(core >= 0),
  adjacent INTEGER NOT NULL CHECK(adjacent >= 0),
  updated_at TEXT NOT NULL,
  CHECK(viewed = included + cut),
  CHECK(included = near + mid + far),
  CHECK(included = core + adjacent),
  FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE CASCADE
) STRICT;

CREATE TABLE gazettes (
  gazette_id TEXT PRIMARY KEY,
  period TEXT NOT NULL,
  title TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('draft', 'published')),
  published_at TEXT,
  asset_manifest_sha256 TEXT NOT NULL CHECK(length(asset_manifest_sha256) = 64 AND asset_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  content_hash TEXT NOT NULL CHECK(length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK((lifecycle_status = 'draft' AND published_at IS NULL) OR (lifecycle_status = 'published' AND published_at IS NOT NULL))
) STRICT;

CREATE INDEX idx_gazettes_lifecycle_period ON gazettes(lifecycle_status, period, gazette_id);

CREATE TABLE gazette_assets (
  gazette_id TEXT NOT NULL,
  relative_path TEXT NOT NULL CHECK(
    relative_path <> '' AND substr(relative_path, 1, 1) <> '/'
    AND instr(relative_path, '\\') = 0
    AND relative_path <> '..' AND relative_path NOT LIKE '../%'
    AND relative_path NOT LIKE '%/../%' AND relative_path NOT LIKE '%/..'
  ),
  sha256 TEXT NOT NULL CHECK(length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
  bytes INTEGER NOT NULL CHECK(bytes >= 0),
  media_type TEXT NOT NULL,
  PRIMARY KEY (gazette_id, relative_path),
  FOREIGN KEY (gazette_id) REFERENCES gazettes(gazette_id) ON DELETE CASCADE
) STRICT;

CREATE VIEW pub_health_v1 AS
SELECT
  1 AS healthy,
  (SELECT COUNT(*) FROM issues WHERE lifecycle_status = 'published') AS published_issue_count,
  (SELECT COUNT(*) FROM gazettes WHERE lifecycle_status = 'published') AS published_gazette_count,
  (SELECT MAX(sequence) FROM content_releases) AS release_sequence;

CREATE VIEW pub_issues_v1 AS
SELECT issue_id, issue_date, issue_number, title, brief, published_at, publication_origin,
       empty_reason, content_hash, created_at, updated_at
FROM issues
WHERE lifecycle_status = 'published';

CREATE VIEW pub_issue_materials_v1 AS
SELECT im.issue_id, im.material_id, im.sort_order, im.perimeter, im.verdict, im.summary,
       im.agpm_takeaway, im.brief, im.theses_json, im.trend_notes, im.flags_json,
       im.key_material, im.signal_score, im.signal_strength, im.created_at, im.updated_at,
       m.title, m.url, m.canonical_url, m.source_name, m.published_at AS material_published_at,
       m.publication_date_status, m.content_hash AS material_content_hash
FROM issue_materials AS im
JOIN issues AS i ON i.issue_id = im.issue_id
JOIN materials AS m ON m.material_id = im.material_id
WHERE i.lifecycle_status = 'published';

CREATE VIEW pub_issue_analysis_v1 AS
SELECT a.issue_id, a.headline, a.analysis_json, a.theses_json, a.brief, a.llm_status,
       a.requested_model, a.effective_model, a.provider, a.prompt_version, a.updated_at
FROM issue_analysis AS a
JOIN issues AS i ON i.issue_id = a.issue_id
WHERE i.lifecycle_status = 'published';

CREATE VIEW pub_material_analysis_v1 AS
SELECT a.issue_id, a.material_id, a.short_text, a.agpm_angle, a.llm_status,
       a.requested_model, a.effective_model, a.provider, a.prompt_version, a.updated_at
FROM material_analysis AS a
JOIN issues AS i ON i.issue_id = a.issue_id
WHERE i.lifecycle_status = 'published';

CREATE VIEW pub_stats_v1 AS
SELECT s.issue_id, s.viewed, s.included, s.cut, s.near, s.mid, s.far, s.core,
       s.adjacent, s.updated_at
FROM daily_stats AS s
JOIN issues AS i ON i.issue_id = s.issue_id
WHERE i.lifecycle_status = 'published';

CREATE VIEW pub_gazettes_v1 AS
SELECT gazette_id, period, title, published_at, asset_manifest_sha256, content_hash,
       created_at, updated_at
FROM gazettes
WHERE lifecycle_status = 'published';

CREATE VIEW pub_search_documents_v1 AS
SELECT im.issue_id || ':' || im.material_id AS document_id, im.issue_id, i.issue_date,
       im.material_id, m.title, coalesce(im.summary, m.summary, '') AS summary,
       coalesce(im.agpm_takeaway, m.agpm_takeaway, '') AS agpm_takeaway,
       coalesce(m.source_name, '') AS source_name, m.url
FROM issue_materials AS im
JOIN issues AS i ON i.issue_id = im.issue_id
JOIN materials AS m ON m.material_id = im.material_id
WHERE i.lifecycle_status = 'published';

CREATE VIRTUAL TABLE published_materials_fts USING fts5(
  document_id UNINDEXED,
  issue_id UNINDEXED,
  issue_date UNINDEXED,
  material_id UNINDEXED,
  title,
  summary,
  agpm_takeaway,
  source_name,
  url UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE VIEW pub_material_rubrics_v1 AS
SELECT mr.issue_id, mr.material_id, mr.rubric_id, r.title, r.sort_order
FROM material_rubrics AS mr
JOIN issues AS i ON i.issue_id = mr.issue_id
JOIN rubrics AS r ON r.rubric_id = mr.rubric_id
WHERE i.lifecycle_status = 'published';

CREATE VIEW pub_material_quality_v1 AS
SELECT q.issue_id, q.material_id, q.publication_date_status, q.issue_date_delta_days,
       q.severity, q.review_status
FROM material_quality AS q
JOIN issues AS i ON i.issue_id = q.issue_id
WHERE i.lifecycle_status = 'published';

CREATE VIEW pub_gazette_assets_v1 AS
SELECT a.gazette_id, a.relative_path, a.sha256, a.bytes, a.media_type
FROM gazette_assets AS a
JOIN gazettes AS g ON g.gazette_id = a.gazette_id
WHERE g.lifecycle_status = 'published';

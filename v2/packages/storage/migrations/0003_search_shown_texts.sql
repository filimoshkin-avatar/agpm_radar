-- Search must find exactly the texts a card shows. Since 02.09.2026 a card shows the
-- model's short text and AgPM angle when its LLM analysis succeeded, and the rule-based
-- brief/summary and takeaway otherwise; the search projection indexed only the
-- rule-based texts, so a reader could search a word seen on a card and get nothing, or
-- get a hit whose visible text never mentions the word. Same name and columns: the API
-- pins the view by name and checks its row count against the FTS table.

DROP VIEW pub_search_documents_v1;

CREATE VIEW pub_search_documents_v1 AS
SELECT im.issue_id || ':' || im.material_id AS document_id, im.issue_id, i.issue_date,
       im.material_id, m.title,
       CASE
         WHEN ma.llm_status = 'success' AND coalesce(ma.short_text, '') <> '' THEN ma.short_text
         ELSE coalesce(nullif(im.brief, ''), nullif(im.summary, ''), m.summary, '')
       END AS summary,
       CASE
         WHEN ma.llm_status = 'success' AND coalesce(ma.agpm_angle, '') <> '' THEN ma.agpm_angle
         ELSE coalesce(nullif(im.agpm_takeaway, ''), m.agpm_takeaway, '')
       END AS agpm_takeaway,
       coalesce(m.source_name, '') AS source_name, m.url
FROM issue_materials AS im
JOIN issues AS i ON i.issue_id = im.issue_id
JOIN materials AS m ON m.material_id = im.material_id
LEFT JOIN material_analysis AS ma
  ON ma.issue_id = im.issue_id AND ma.material_id = im.material_id
WHERE i.lifecycle_status = 'published';

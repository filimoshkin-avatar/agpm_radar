BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Путь до выпуска: чем утверждение связано с радаром
--
-- Цепочка доверия в дизайн-системе кончается словом ПЕРВОИСТОЧНИК, и до сих пор
-- на нём же кончалась и база: карточка доказательства показывала документ и
-- замолкала. Между тем связь есть и была с самого начала - `issue_perimeter_members`
-- держит, какой выпуск радара выбрал этот документ, каким материалом и в какой
-- периметр. Читателю она не доходила: роль обслуживания видит только схему
-- `agent`, а вьюхи там для неё не было.
--
-- Сужено тем же способом, что `statement_vector` (026) и `entity` (030): путь
-- показывается только для утверждения, которое читатель и так может прочесть.
--
-- Замерено на проде 2026-08-25: путь до выпуска есть у 6 372 утверждений из
-- 11 759 - 54 %. Остальные пришли из канона, wiki и операторского импорта, и
-- для них честный ответ - «этот материал в выпуск радара не входил», а не
-- выдуманный номер. Вьюха поэтому не LEFT JOIN: отсутствие строки и есть ответ.
--
-- Один документ может попасть в несколько выпусков, и один выпуск может прийти
-- из двух источников сразу (`v2_content_release` и `legacy_radar_db`). Группировка
-- по (утверждение, дата выпуска) сводит вторую двойственность к одной строке и
-- считает источники, чтобы расхождение было видно, а не спрятано.
-- ---------------------------------------------------------------------------

CREATE VIEW agent.statement_trail AS
SELECT claims.claim_id,
       members.issue_date,
       max(members.issue_number) AS issue_number,
       min(members.perimeter) AS perimeter,
       min(members.title) AS material_title,
       min(members.canonical_url) AS material_url,
       bool_or(members.key_material) AS key_material,
       count(DISTINCT members.perimeter_source_id) AS source_count
FROM kx.claims AS claims
JOIN kx.document_versions AS versions ON versions.version_id = claims.version_id
JOIN kx.issue_perimeter_members AS members ON members.document_id = versions.document_id
WHERE EXISTS (
    SELECT 1 FROM agent.statement
    WHERE statement.claim_id = claims.claim_id
)
GROUP BY claims.claim_id, members.issue_date;

COMMENT ON VIEW agent.statement_trail IS
    'Какой выпуск радара выбрал материал, из которого взято утверждение: дата, '
    'номер, периметр и заголовок материала. Утверждения не из радара - канон, '
    'wiki, операторский импорт - строк здесь не имеют, и это ответ, а не пробел.';

GRANT SELECT ON agent.statement_trail TO radar_kb_public;

UPDATE metadata SET value = '32'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;

BEGIN;

SET search_path = kx, public;

-- ---------------------------------------------------------------------------
-- Цепочка проходит и не оставляет следа
--
-- Четыре таймера кормят базу: perimeter-poll забирает новые материалы радара,
-- ingest их достаёт и разбирает, catch-up читает, извлекает, связывает и
-- называет, embed считает векторы. Каждый пишет свой JSON в журнал systemd и
-- ничего - в базу. Поэтому на вопрос «когда база последний раз синхронизировалась»
-- ответить было нечем: в схеме этой отметки просто не существовало, и макетное
-- «СИНХР. 06:00 UTC» во фронте пришлось бы выдумать.
--
-- Здесь не «время последнего прохода», а строка на каждый проход: когда начался,
-- когда кончился, чем кончился и что сделал. Одно число выводится из строк, а не
-- заменяет их: проход, который упал, должен быть виден как упавший, иначе
-- «синхронизировано» будет означать «последний раз, когда что-то запускалось».
-- Тот же урок, что и с пассом, который писал success при calls: 0.
--
-- Строка не иммутабельна намеренно: её открывают в начале прохода и закрывают в
-- конце. Так же устроен `processing_runs`, и по той же причине - падение
-- посередине должно остаться видимым как `running`, которое никто не закрыл, а
-- не исчезнуть вместе с транзакцией.
-- ---------------------------------------------------------------------------

CREATE TABLE chain_passes (
    pass_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Шаг цепочки, а не имя команды: команду можно переименовать, шаг - это то,
    -- что читатель понимает под «база обновилась».
    step text NOT NULL CHECK (step IN ('perimeter', 'ingest', 'knowledge', 'embedding')),
    command text NOT NULL,
    release_id text NOT NULL DEFAULT '',
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    outcome text NOT NULL DEFAULT 'running'
        CHECK (outcome IN ('running', 'succeeded', 'failed')),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

COMMENT ON TABLE chain_passes IS
    'Один проход одного шага суточной цепочки: когда начался, когда кончился, '
    'чем кончился и что сделал. Отсюда - и только отсюда - берётся ответ на '
    '«когда база последний раз синхронизировалась».';

CREATE INDEX chain_passes_step_idx ON chain_passes (step, finished_at DESC);

-- ---------------------------------------------------------------------------
-- Что из этого видит читатель
--
-- Не журнал, а две отметки на шаг: когда шаг в последний раз прошёл удачно и
-- когда его в последний раз вообще пробовали. Разница между ними и есть ответ
-- на «а точно ли свежо»: если попытка новее удачи, шаг падает, и говорить
-- «синхронизировано только что» было бы враньём.
--
-- `detail` наружу не выходит: там счётчики вызовов и бюджеты, то есть устройство
-- прохода, а не состояние базы.
-- ---------------------------------------------------------------------------

CREATE VIEW agent.sync AS
SELECT step,
       max(finished_at) FILTER (WHERE outcome = 'succeeded') AS succeeded_at,
       max(finished_at) AS attempted_at,
       count(*) FILTER (WHERE outcome = 'failed'
                          AND finished_at > coalesce(
                              (SELECT max(inner_pass.finished_at)
                                 FROM kx.chain_passes AS inner_pass
                                WHERE inner_pass.step = passes.step
                                  AND inner_pass.outcome = 'succeeded'),
                              '-infinity'::timestamptz)) AS failures_since
FROM kx.chain_passes AS passes
WHERE finished_at IS NOT NULL
GROUP BY step;

COMMENT ON VIEW agent.sync IS
    'По одной строке на шаг цепочки: когда он в последний раз прошёл удачно, '
    'когда его пробовали и сколько раз он упал с тех пор. База свежа настолько, '
    'насколько свеж её самый отставший шаг.';

GRANT SELECT ON agent.sync TO radar_kb_public;

UPDATE metadata SET value = '33'::jsonb, updated_at = clock_timestamp()
WHERE key = 'schema_version';

COMMIT;

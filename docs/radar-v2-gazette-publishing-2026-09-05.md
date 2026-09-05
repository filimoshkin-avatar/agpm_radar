# Как выпустить номер газеты

С 05.09.2026 номер — опубликованное содержимое, а не файл внутри приложения.
Выпуск нового номера не требует ни выката приложения, ни правки разметки.

## Одной командой

```
cd /mnt/vdd/Radar/v2
src=/root/.openclaw-projectmanager/workspace/state/radar-v2/source
db="$src/$(python3 -c "import json;print(json.load(open('$src/active.json'))['database'])")"
work=/root/.openclaw-projectmanager/workspace/state/radar-v2/gazette-$(date -u +%Y%m%d)

PYTHONPATH=. .venv/bin/python -m tools.build_gazette_candidate \
  --source-db "$db" \
  --html /путь/к/номеру.html \
  --period 2026-10 \
  --issue-date 2026-10-01 \
  --candidate-id cand_gazette_202610 \
  --root "$work/build"
```

Инструмент сам берёт из базы, есть ли уже номер этого периода, и его текущий
хэш; заголовок читает из `<title>` документа; путь ассета собирает как
`gazettes/<период>/index-<12 hex sha256>.html`. Печатает JSON с путями пакета и
временной базы — они нужны следующей команде.

```
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PYTHONPATH=. .venv/bin/python -m apps.publisher_runner \
  --package   "$work/build/packages/cand_gazette_202610" \
  --candidate-staging "$work/build/staging/gazette.sqlite" \
  --source-root /root/.openclaw-projectmanager/workspace/state/radar-v2/source \
  --work-root  /root/.openclaw-projectmanager/workspace/state/radar-v2/publisher \
  --application-release-id "$(curl -s https://radar.agpm.space/api/health | python3 -c 'import json,sys;print(json.load(sys.stdin)["applicationReleaseId"])')" \
  --created-at "$now" --finished-at "$now" --duration-ms 60000 \
  --ssh-host radar-v2-deploy@radar.agpm.space \
  --ssh-identity /root/.ssh/radar_v2_publisher_stage13 \
  --result "$work/result.json" --report "$work/report.json"
```

Проверка: `curl -s https://radar.agpm.space/api/gazettes` — новый номер первым,
его `url` открывается двухсоткой, на сайте в разделе «Газета» он в рамке и
первым в архиве с пометкой «ТЕКУЩИЙ».

## Три вещи, которые стоит знать

**Адрес номера несёт его содержимое.** `/gazettes/*` отдаётся с
`public, max-age=31536000, immutable`, поэтому ассет по одному адресу
неизменяем: активатор откажет с «immutable asset path contains different
bytes», если положить по нему другие байты. Поэтому имя файла содержит первые
двенадцать hex от sha256 его содержимого — то же правило, что `?v=` у
`app.mjs`. Ревизия номера — это новый адрес, и прежний честно отдаёт 404, когда
его строка уходит из таблицы ассетов.

**`--issue-date` — дата, которую увидит читатель.** Она попадает в
`gazettes.published_at`, возвращается из `/api/gazettes` и печатается в архиве
как «3 авг». Это не время запуска: `date -u` здесь молча переименовал бы старый
номер в сегодняшний. Инструмент требует, чтобы дата лежала внутри своего
периода.

**Номер обязан быть самодостаточным.** Валидатор отвергает любую внешнюю
зависимость: ни `<script>`, ни ссылки на шрифты, ни картинки с чужого хоста.
Шрифты вшиваются как `data:` — так сделаны оба нынешних номера, по двенадцать
начертаний в каждом. Внешние ссылки разрешены только у `<a>`, и `target=_blank`
требует `rel="noopener noreferrer"`.

Августовская ревизия, которая лежала в базе с августа, сегодняшнюю валидацию бы
не прошла: она тянула шрифты Google. Она попала туда сидом Stage 11, минуя
кандидатный путь, и была заменена 05.09.2026.

## Что происходит с прежним номером периода

Кандидат перечисляет все ассеты номера, и `_add_gazette_state` удаляет строки
тех, которых в нём нет. Файл прежней ревизии остаётся на диске — ассеты
неизменяемы, — но из базы уходит, и служба на его адрес отвечает 404, потому что
маршрут `/gazettes/*` проверяет каждый байт по базе.

## Если что-то пошло не так

Публикация идёт через тот же durable state machine, что и суточный выпуск:
повтор с тем же кандидатом воспроизводит сохранённый результат, а не применяет
дельту второй раз. Ошибка активатора приходит его собственным JSON в stdout;
`RemoteOrchestrationError` показывает только stderr, где обычно лежит безобидное
предупреждение ssh про псевдотерминал, — настоящую причину искать в
`/var/lib/radar-v2/audit/content/` на хосте и в journald.

# Миграция 0004: убрать поисковый индекс — runbook

Убирается `published_materials_fts` вместе с теневыми таблицами и представление
`pub_search_documents_v1`, которое существовало ради неё одной. Решение
владельца и его основания — ADR-0014.

Миграция отрепетирована дважды на копиях продовой базы, последний раз
05.09.2026 после публикаций газеты: применяется только `0004`, хэш логического
состояния не меняется и совпадает с указателем релиза, объектов поиска в схеме
не остаётся, мигрированную базу служба открывает и отвечает на девять
эндпоинтов.

## Порядок. Он обязателен, и он не тот, что кажется

**Служба → активатор → миграция.** Не наоборот.

Старая служба сверяет число строк индекса с представлением при **каждом**
переоткрытии базы (`_connect_pointer`, «published search projection parity
failed»), а новый активатор индекс больше не перестраивает. Обратный порядок
оставляет окно, в котором одна суточная публикация добавляет строку в
представление, не добавляя в индекс, — и старая служба после смены указателя
отвечает **503 на весь сайт** и сама уже не поднимется: следующая публикация
под новым активатором ничего не чинит.

Новая служба не проверяет чётность вовсе и работает и со свежим индексом, и с
устаревшим, и без него. Поэтому в правильном порядке опасного сочетания не
возникает ни на минуту.

Всё равно делать одним окном и **вне суточного окна публикации** (цепочка
проходит около 05:30 UTC): между шагами 2 и 3 индекс устаревает, и хотя ничто
его не читает, лишний день с расходящейся производной таблицей ни к чему.

## Состояние на 05.09.2026, 19:00 UTC

Шаги 0 и 1 выполнены: ветка прочитана вторым агентом, исправлена, смержена
(`01f4fa1`), релиз `app_release_20260905_01f4fa1` собран и выкачен — обе роли,
служба перезапущена, здоровье его называет, восемь маршрутов отвечают.

Осталось: шаги 2–5. Активатор пока `stage14-final-8c9a4b1`, база пока с
индексом (374 строки). **Это устойчивое сочетание, а не половина работы:** новая
служба чётность не проверяет, старый активатор индекс перестраивает, и суточная
публикация проходит как обычно. Так можно стоять сколько угодно.

Готово и ждёт переключения указателя:
`/opt/radar-v2-activator/releases/drop-index-01f4fa1` — импорт под закреплённым
рантаймом проверен, кода индекса в дереве нет, права выставлены,
`current.before-drop-index-01f4fa1` записан. Бандл миграций и манифест
совместимости от этого же релиза — в
`/var/lib/radar-v2/incoming/application/app_release_20260905_01f4fa1/stage/`.

Что остановило: применение миграции к production и переключение указателя
активатора не пропустила политика сессии.

## 0. Ревью и мерж

Ветку читает второй агент. `main` ушёл вперёд, поэтому не `merge --ff-only` от
старой базы, а ребейз:

```
cd /mnt/vdd/Radar
git rebase main v2/drop-search-index-20260905
git merge --ff-only v2/drop-search-index-20260905
```

## 1. Служба

Собрать релиз **с коммита мержа** и выкатить обе роли. Прежний релиз узнать у
хоста, а не из этой строки:

```
ssh … 'readlink /opt/radar-v2-api/current'
v2/scripts/deploy_application_release.sh <коммит мержа>
```

Скрипт сверяет каждый файл с `MANIFEST.json`, ждёт, пока здоровье назовёт новый
релиз, откатывает сам, если не дождался, и проверяет токены ассетов дважды —
локально до отправки и на хосте после переключения.

Проверка: `/api/health` называет новый релиз, `/api/gazettes` отдаёт два номера,
главная открывается.

Откат: указатель на прежний релиз плюс рестарт. **Безопасен только до шага 3.**

## 2. Активатор

Активатор — четвёртая копия кода на хосте, и её никто не обновлял с 19.08.2026.
Состав файлов взять из текущего релиза активатора, добавить
`packages/contracts/analysis.py` (в августовском составе её не было, и без неё
дерево не импортируется) и все четыре миграции:

```
ssh … 'find /opt/radar-v2-activator/releases/<текущий>/ -type f | sed "s#.*/<текущий>/##" | grep -v __pycache__ | sort'
```

Разложить в `/opt/radar-v2-activator/releases/<имя>.new`, убедиться, что в
дереве не осталось `rebuild_and_check_fts` и `published_materials_fts`, и
прогнать импорт под закреплённым рантаймом **до** переключения:

```
ssh … 'D=/opt/radar-v2-activator/releases/<имя>.new;
  PYTHONHOME=/opt/radar-v2-runtime/current PYTHONPATH="$D" \
  LD_LIBRARY_PATH=/opt/radar-v2-runtime/current/lib/x86_64-linux-gnu \
  /opt/radar-v2-runtime/current/bin/python3.12 -c "import tools.radar_remote_activator"'
```

Затем `chown -R root:root`, каталоги 0555, файлы 0444, `mv -T` и переключение
`current` через `.new` + `mv -T`. Откат: указатель на `stage14-final-8c9a4b1`.

## 3. Миграция продовой базы

Снять свежую копию (если с последнего снятия прошла публикация — обязательно,
иначе миграция уедет на устаревший снимок):

```
ssh … 'W=/var/lib/radar-v2/migration-0004; P=/var/lib/radar-v2/content/active.json;
  db=$(python3 -c "import json;print(json.load(open(\"$P\"))[\"database\"])");
  rm -f "$W/staging-0004.sqlite";
  cp "/var/lib/radar-v2/content/$db" "$W/staging-0004.sqlite";
  chmod 0600 "$W/staging-0004.sqlite"; chown root:root "$W/staging-0004.sqlite";
  python3 -c "import json;print(json.load(open(\"$P\"))[\"stateHash\"])" > "$W/expected-state-hash.txt"'
```

Бандл миграций и манифест совместимости распаковать из **того же** пакета, что
выкачен на шаге 1: манифест привязан к идентификатору релиза.

**Зафиксировать одно время на оба хоста** и ввести его тем же литералом и здесь,
и в шаге 4:

```
STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
```

Иначе `schema_migrations.applied_at` и строка в `application_compatibility`
разойдутся между продом и источником. Обе таблицы вне хэша логического
состояния, но **внутри** табличных хэшей дайджеста, и `tools/compare_databases.py`
— единственный инструмент, доказывающий тождество источника и реплики, — станет
печатать `equivalent: false` навсегда.

```
ssh … "cd /var/lib/radar-v2/migration-0004/radar-v2-migrations &&
  PYTHONHOME=/opt/radar-v2-runtime/current PYTHONPATH=. \
  LD_LIBRARY_PATH=/opt/radar-v2-runtime/current/lib/x86_64-linux-gnu \
  /opt/radar-v2-runtime/current/bin/python3.12 -m apps.migration_runner \
    --staging-database /var/lib/radar-v2/migration-0004/staging-0004.sqlite \
    --compatibility-manifest /var/lib/radar-v2/migration-0004/compatibility-manifest.json \
    --migrations packages/storage/migrations \
    --lock-root /var/lib/radar-v2/migration-0004/lock \
    --activated-at '$STAMP' \
    --expected-state-hash \$(cat /var/lib/radar-v2/migration-0004/expected-state-hash.txt)"
```

Отчёт обязан показать `appliedMigrations: ["0004"]` и **тот же** `stateHash`,
что в указателе. Другой хэш — не продолжать.

**Запускать из каталога пакета.** `-m apps.migration_runner` кладёт текущий
каталог первым в `sys.path`, и запуск из репозитория подхватит старый код вместо
кода бандла. Проверено 05.09.2026: так и произошло, и старый код честно упал на
`no such table: published_materials_fts` — то же падение, что ждало бы
необновлённый активатор.

Установить мигрированную базу и переключить указатель:

```
ssh … 'set -eu; W=/var/lib/radar-v2/migration-0004; C=/var/lib/radar-v2/content;
  name=$(python3 -c "import secrets;print(secrets.token_hex(16))").sqlite;
  cp "$W/staging-0004.sqlite" "$C/releases/$name";
  chown radar-v2-api:radar-v2-api "$C/releases/$name"; chmod 0600 "$C/releases/$name";
  cp "$C/active.json" /var/lib/radar-v2/backups/active.before-0004.json;
  python3 -c "
import json, sys
p = json.load(open(sys.argv[1])); p[\"database\"] = sys.argv[2]
open(sys.argv[1] + \".next\", \"w\").write(json.dumps(p, separators=(\",\", \":\")) + \"\n\")
" "$C/active.json" "releases/$name";
  chown radar-v2-api:radar-v2-api "$C/active.json.next"; chmod 0600 "$C/active.json.next";
  mv -T "$C/active.json.next" "$C/active.json"; cat "$C/active.json"'
```

`releaseId` и `stateHash` не меняются — меняется только имя файла базы. Служба
сама заметит смену указателя и переоткроет базу.

## 4. То же с базой источника

На контрол-хосте, `/root/.openclaw-projectmanager/workspace/state/radar-v2/source`:
снять копию, применить ту же миграцию тем же бандлом **с тем же `STAMP`**,
положить рядом и переписать `active.json`. Сделать в тот же день.

## 5. Юнит

Шаблон `v2/deploy/templates/radar-v2-api.service` уже доведён до уровня юнитов
kx. На хосте юнит ставился руками, поэтому дописать те же директивы в
`/etc/systemd/system/radar-v2-api.service`:

```
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
PrivateIPC=true
PrivateMounts=true
KeyringMode=private
SocketBindDeny=any
SocketBindAllow=ipv4:tcp:8765
```

Порядок: `systemd-analyze security radar-v2-api.service` (было **2.7 OK**) →
правка → `daemon-reload` → `restart` → `is-active` → `curl 127.0.0.1:8765/api/health`
→ `systemd-analyze security` снова. Фильтр системных вызовов — самая рискованная
строка. Все юниты kx на этом хосте несут ровно эту пару с августа под тем же
перемещаемым CPython 3.12.3.

Откат: убрать дописанные строки, `daemon-reload`, `restart`.

## Откат идёт в обратном порядке

После шага 3 шаги перестают быть независимыми: старый код службы требует
`pub_search_documents_v1` и `published_materials_fts` в списке чтения и на
мигрированной базе даст `active database is missing published API projections`
ещё до всякой чётности.

Порядок отката: **база → служба → активатор**, то есть 3 → 1 → 2. Откат службы
или активатора в одиночку после шага 3 сайт не поднимет.

## Проверка после всего

```
curl -sS https://radar.agpm.space/api/health
curl -sS -o /dev/null -w '%{http_code}\n' https://radar.agpm.space/api/latest
curl -sS 'https://radar.agpm.space/api/search?q=agent&period=30d' | head -c 200
curl -sS https://radar.agpm.space/api/gazettes
```

И на следующее утро — суточная цепочка: `combined-report.json` за новую дату,
`comparisonVerdict.status` не `unexplained`.

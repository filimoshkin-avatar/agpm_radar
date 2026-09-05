# Миграция 0004: убрать поисковый индекс — runbook

Ветка `v2/drop-search-index-20260905`, коммит `73fb6d3`. Код готов, гейты зелёные,
миграция отрепетирована на копии продовой базы. **Не смержено и не выкачено:**
применение миграции к production заблокировано политикой сессии 05.09.2026, и
ветку ещё не читал второй агент.

Всё, что перечислено ниже, уже подготовлено на Local Ru и лежит на диске.
Продовое состояние на момент остановки — целиком на смерженном `3d59323`,
база не тронута, индекс в ней на месте (374 строки).

## Что уже сделано и лежит готовым

| Что | Где | Состояние |
| --- | --- | --- |
| Пакет релиза | `/var/lib/radar-v2/incoming/application/app_release_20260905_73fb6d3/` | распакован, суммы сверены |
| Роль api | `/opt/radar-v2-api/releases/app_release_20260905_73fb6d3` | установлена, `root:radar-v2-api` 0550/0440, указатель НЕ на неё |
| Активатор | `/opt/radar-v2-activator/releases/drop-search-index-73fb6d3` | установлен, импорт проверен под закреплённым рантаймом, указатель НЕ на него |
| Бандл миграций | `/var/lib/radar-v2/migration-0004/radar-v2-migrations/` | распакован, все четыре `.sql` |
| Манифест совместимости | `/var/lib/radar-v2/migration-0004/compatibility-manifest.json` | 0600 |
| Копия продовой базы | `/var/lib/radar-v2/migration-0004/staging-0004.sqlite` | 0600 root, снята с активного релиза |

Репетиция на копии базы источника прошла: применилась только `0004`, хэш
логического состояния не изменился (`30252da9…`), объектов поиска не осталось,
новая служба открыла такую базу и ответила на восемь запросов.

## Порядок. Он обязателен

Сначала **весь код**, потом миграция. Активатор на проде несёт собственную копию
`delta/engine.py` и `storage/hashing.py` от 19.08.2026 и вызывает
`verify_database` на применении дельты; со старым кодом и базой без индекса
суточная публикация упала бы. Новый код работает и с индексом, и без него, —
поэтому обратный порядок безопасен, а прямой нет.

### 0. Ревью и мерж

```
# второй агент читает main...v2/drop-search-index-20260905
git -C /mnt/vdd/Radar merge --ff-only v2/drop-search-index-20260905
```

### 1. Активатор

```
ssh -i /root/.ssh/local_ru_admin root@radar.agpm.space \
  'ln -sfn /opt/radar-v2-activator/releases/drop-search-index-73fb6d3 /opt/radar-v2-activator/current.new \
   && mv -T /opt/radar-v2-activator/current.new /opt/radar-v2-activator/current \
   && readlink /opt/radar-v2-activator/current'
```

Откат: то же с `stage14-final-8c9a4b1` (симлинк `current.before-drop-search-index`
уже записан).

### 2. Служба

```
ssh -i /root/.ssh/local_ru_admin root@radar.agpm.space \
  'ln -sfn releases/app_release_20260905_73fb6d3 /opt/radar-v2-api/current.new \
   && mv -T /opt/radar-v2-api/current.new /opt/radar-v2-api/current \
   && systemctl restart radar-v2-api.service && sleep 4 \
   && curl -s http://127.0.0.1:8765/api/health'
```

Здоровье обязано назвать `app_release_20260905_73fb6d3`. Откат: то же с
`app_release_20260905_3d59323` плюс рестарт.

### 3. Миграция продовой базы

Копия уже снята. Если активная база с тех пор сменилась (прошла публикация) —
снять заново, иначе миграция уедет на устаревший снимок:

```
ssh … 'W=/var/lib/radar-v2/migration-0004; P=/var/lib/radar-v2/content/active.json;
  db=$(python3 -c "import json;print(json.load(open(\"$P\"))[\"database\"])");
  rm -f "$W/staging-0004.sqlite";
  cp "/var/lib/radar-v2/content/$db" "$W/staging-0004.sqlite";
  chmod 0600 "$W/staging-0004.sqlite";
  python3 -c "import json;print(json.load(open(\"$P\"))[\"stateHash\"])" > "$W/expected-state-hash.txt"'
```

Сама миграция (это шаг, который заблокировала политика сессии):

```
ssh … 'cd /var/lib/radar-v2/migration-0004/radar-v2-migrations &&
  PYTHONHOME=/opt/radar-v2-runtime/current PYTHONPATH=. \
  LD_LIBRARY_PATH=/opt/radar-v2-runtime/current/lib/x86_64-linux-gnu \
  /opt/radar-v2-runtime/current/bin/python3.12 -m apps.migration_runner \
    --staging-database /var/lib/radar-v2/migration-0004/staging-0004.sqlite \
    --compatibility-manifest /var/lib/radar-v2/migration-0004/compatibility-manifest.json \
    --migrations packages/storage/migrations \
    --lock-root /var/lib/radar-v2/migration-0004/lock \
    --activated-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --expected-state-hash "$(cat /var/lib/radar-v2/migration-0004/expected-state-hash.txt)"'
```

Отчёт обязан показать `appliedMigrations: ["0004"]` и **тот же** `stateHash`, что
в указателе. Другой хэш — не продолжать: индекс в состояние не входил, и его
удаление не имеет права его менять.

**Запускать из каталога пакета.** `-m apps.migration_runner` берёт текущий
каталог первым в `sys.path`, и запуск из `/mnt/vdd/Radar/v2` подхватит старый код
репозитория вместо кода бандла. Проверено 05.09.2026: так и произошло, и старый
код честно упал на `no such table: published_materials_fts` — то же падение, что
ждало бы необновлённый активатор.

### 4. Установить мигрированную базу и переключить указатель

```
ssh … 'set -eu; W=/var/lib/radar-v2/migration-0004; C=/var/lib/radar-v2/content;
  name=$(python3 -c "import secrets;print(secrets.token_hex(16))").sqlite;
  cp "$W/staging-0004.sqlite" "$C/releases/$name";
  chown radar-v2-api:radar-v2-api "$C/releases/$name"; chmod 0600 "$C/releases/$name";
  cp "$C/active.json" "/var/lib/radar-v2/backups/active.before-0004.json";
  python3 - "$C/active.json" "releases/$name" <<PY
import json, sys
p = json.load(open(sys.argv[1])); p["database"] = sys.argv[2]
open(sys.argv[1] + ".next", "w").write(json.dumps(p, separators=(",", ":")) + "\n")
PY
  chown radar-v2-api:radar-v2-api "$C/active.json.next"; chmod 0600 "$C/active.json.next";
  mv -T "$C/active.json.next" "$C/active.json"; cat "$C/active.json"'
```

`releaseId` и `stateHash` не меняются — меняется только имя файла базы. Служба
сама заметит смену указателя и переоткроет базу; проверить `/api/health` и
`/api/latest`. Откат: вернуть `backups/active.before-0004.json` на место.

### 5. То же с базой источника

На контрол-хосте, `/root/.openclaw-projectmanager/workspace/state/radar-v2/source`:
снять копию, применить ту же миграцию тем же бандлом, положить рядом и переписать
`active.json`. Порядок с продом не важен: индекс не реплицируется и в дельту не
входит, поэтому расхождение схем между источником и продом дельту не ломает.
Сделать в тот же день, чтобы DR не разъехался.

### 6. Юнит

Шаблон `v2/deploy/templates/radar-v2-api.service` уже доведён до уровня юнитов kx.
На хосте юнит ставился руками, поэтому те же директивы дописать в
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

Порядок: `systemd-analyze security radar-v2-api.service` (сейчас **2.7 OK**) →
правка → `systemctl daemon-reload` → `systemctl restart radar-v2-api` →
`systemctl is-active` → `curl 127.0.0.1:8765/api/health` → `systemd-analyze
security` снова. Фильтр системных вызовов — самая рискованная строка: слишком
узкий не даёт службе стартовать. Все юниты kx на этом хосте несут ровно эту пару
с августа и работают под тем же перемещаемым CPython 3.12.3, что и API.

Откат: убрать дописанные строки, `daemon-reload`, `restart`.

## Проверка после всего

```
curl -sS https://radar.agpm.space/api/health          # applicationReleaseId = …_73fb6d3
curl -sS -o /dev/null -w '%{http_code}\n' https://radar.agpm.space/api/latest
curl -sS 'https://radar.agpm.space/api/search?q=agent&period=30d' | head -c 200
```

И на следующее утро — суточная цепочка: `combined-report.json` за новую дату,
`comparisonVerdict.status` не `unexplained`.

## Чего в базе не станет

`published_materials_fts` с пятью теневыми таблицами и представление
`pub_search_documents_v1`. Ни то, ни другое не читает ни один запрос API с
02.09.2026: поиск идёт по тексту карточки в приложении. Логическое состояние
базы их не включает, поэтому указатель релиза остаётся верным.

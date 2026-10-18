# Отчёт учёта времени

В аналитике пространства появилась вкладка **Учёт времени**:
`/<workspaceSlug>/analytics/time-tracking`.

Отчёт читает существующую таблицу `ext_issue_worklogs` (`IssueWorkLog`):
`duration` — целые минуты, `logged_by` — автор записи, `created_at` — время её
добавления. Отдельной даты выполненной работы нет. Изменения схемы, пересчёт
старых записей и перенос данных не нужны.

- Общий итог — сумма минут отфильтрованных записей; число записей — их количество.
- Сводка группируется по автору; детализация — по задаче и автору, а не исполнителям.
- Период относится к `created_at`: от местной полуночи первого дня включительно
  до местной полуночи дня после последнего исключительно. Часовой пояс IANA
  задаётся в форме (по умолчанию пояс браузера); API без этого параметра использует
  пояс профиля. Учитываются переходы на летнее/зимнее время. Пустые даты означают
  отсутствие ограничения с соответствующей стороны.
- Архивные задачи/проекты и проекты с отключённым учётом включаются для сохранения
  истории. Мягко удалённые записи, задачи и проекты исключены сразу, независимо
  от фонового каскадного удаления. Физически удалённые данные не восстанавливаются.
- Удаление пользователя оставляет `logged_by=NULL` (`SET_NULL`); такие записи
  объединены в группу «Удалённый пользователь». Неактивные сохранившиеся авторы
  остаются под своим именем; членство автора в пространстве сейчас не требуется.
- Читать отчёт могут активные участники и администраторы пространства, только по
  проектам со своим активным членством и ролью участника/администратора. Как и в
  API записей времени, администратору пространства разрешена гостевая роль в
  проекте, но членство всё равно обязательно. Списки фильтров используют ту же
  область доступа. Гости пространства не получают отчёт.
- Детализация разбита по 100 групп на страницу, суммы считаются в PostgreSQL.
  CSV содержит все группы при тех же фильтрах, целые минуты и количество записей;
  используется потоковая выдача и защита строк от формул в электронных таблицах.
  Итог детализации нужно суммировать по всем страницам. Отчёт живой: если записи
  меняют между запросами или выгрузками, результат может измениться.

## Проверки разработки

Из корня репозитория, после установки зависимостей:

```bash
pnpm turbo run build --filter='web^...' --concurrency=4
pnpm --filter web test
pnpm --filter web check:types
pnpm --filter web check:lint
pnpm --filter web build
# Только изолированная тестовая БД; не запускать pytest на рабочей БД!
docker compose -p hangar-report-test -f docker-compose-test.yml run --rm --build api-tests \
  pytest plane/tests/contract/app/test_worklog_report_app.py \
         plane/tests/contract/app/test_worklog_app.py
```

Тесты отчёта проверяют автора вместо исполнителей, доступ и роли, чужие
пространства и проекты, неактивное членство, архивы, отключённый учёт,
удалённого автора, пустые результаты, даты и DST, пагинацию, CSV и ограниченное
число запросов при 105 группах детализации. Это не нагрузочный тест рабочей БД.

## Ручное обновление сервера и откат

Эти команды подготовлены по репозиторию; на `/opt/hangar` они не выполнялись.
Содержимое серверного wrapper и фактическая конфигурация Compose из облачной
среды недоступны. Сначала проверить wrapper: он должен передавать аргументы
`config`, `ps`, `up`, `logs` в Compose и сохранять SMTP-настройки. Не заменять его
обычным `docker compose` и не запускать workflow публикации чужих образов.
Ниже обновляются **только api и web**, без остановки БД и остальных сервисов.

Выполнять в одной Bash-сессии после проверки и слияния PR в `preview`:

```bash
set -euo pipefail
cd /opt/hangar
test -x /opt/hangar/runtime/hangar-compose
sed -n '1,240p' /opt/hangar/runtime/hangar-compose
# Остановиться, если wrapper содержит неподходящие команды/не передаёт аргументы.
COMPOSE=/opt/hangar/runtime/hangar-compose
"$COMPOSE" config --services
"$COMPOSE" ps
# Проверить соответствие сервисов api/web/db действующей установке.
test -z "$(git status --porcelain)"
git fetch origin preview
hangar_revision=$(git rev-parse origin/preview)
# Убедиться, что этот commit включает проверенную реализацию отчёта:
git show --stat "$hangar_revision"

umask 077
hangar_backup="/opt/hangar-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$hangar_backup"
git rev-parse HEAD > "$hangar_backup/source-before.txt"
printf '%s\n' "$hangar_revision" > "$hangar_backup/source-after.txt"
tar -C /opt/hangar -czf "$hangar_backup/runtime.tgz" runtime
# Архив содержит конфигурацию, возможно секреты: оставить закрытые права.
docker exec hangar-plane-db-1 sh -ec \
  'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$hangar_backup/database.dump"
test -s "$hangar_backup/database.dump"
docker exec -i hangar-plane-db-1 pg_restore --list \
  < "$hangar_backup/database.dump" > "$hangar_backup/database-contents.txt"

hangar_api_container=$("$COMPOSE" ps -q api)
hangar_web_container=$("$COMPOSE" ps -q web)
test -n "$hangar_api_container" && test -n "$hangar_web_container"
hangar_api_ref=$(docker inspect --format '{{.Config.Image}}' "$hangar_api_container")
hangar_web_ref=$(docker inspect --format '{{.Config.Image}}' "$hangar_web_container")
# Этот способ использует локальные теги. Для digest-only image нужна отдельная
# проверенная Compose override-конфигурация; не продолжать автоматически.
case "$hangar_api_ref $hangar_web_ref" in *'@sha256:'*) exit 1;; esac
test "$hangar_api_ref" != "$hangar_web_ref"
hangar_stamp=$(basename "$hangar_backup")
docker tag "$(docker inspect --format '{{.Image}}' "$hangar_api_container")" "hangar-rollback-api:$hangar_stamp"
docker tag "$(docker inspect --format '{{.Image}}' "$hangar_web_container")" "hangar-rollback-web:$hangar_stamp"
printf '%s\n' "$hangar_api_ref" > "$hangar_backup/api-ref.txt"
printf '%s\n' "$hangar_web_ref" > "$hangar_backup/web-ref.txt"

git switch --detach "$hangar_revision"
docker build --build-arg "HANGAR_SOURCE_REVISION=$hangar_revision" \
  -f apps/api/Dockerfile.api -t "hangar-local-api:$hangar_stamp" apps/api
# Пустые VITE_*_BASE_URL используют тот же origin и существующий proxy.
# Если установка использует отдельные origins, передать её действующие build args.
docker build -f apps/web/Dockerfile.web -t "hangar-local-web:$hangar_stamp" .
# Только после успешной сборки обоих образов меняем локальные рабочие теги.
docker tag "hangar-local-api:$hangar_stamp" "$hangar_api_ref"
docker tag "hangar-local-web:$hangar_stamp" "$hangar_web_ref"
"$COMPOSE" up -d --no-deps --no-build --pull never --force-recreate api web
"$COMPOSE" ps
"$COMPOSE" logs --tail=100 api web
curl --fail --silent --show-error https://plane.iage.net/ > /dev/null
```

Под своей учётной записью открыть аналитику, выбрать период и сверить общий итог,
сводку, все страницы детализации и сумму минут CSV. Отдельно проверить пользователя
без доступа к приватному проекту и прежнее окно Log time в боковой карточке.
HTTP 200 главной страницы само по себе не подтверждает работу отчёта.
Проверить SMTP-конфигурацию существующего `hangar-mail-worker-1`; его пересоздание
для этого изменения не требуется. Не выполнять `down -v`, очистку БД или образов.

Откат образов (в той же сессии; после переподключения задать путь `hangar_backup`
к созданному каталогу):

```bash
COMPOSE=/opt/hangar/runtime/hangar-compose
hangar_stamp=$(basename "$hangar_backup")
hangar_api_ref=$(cat "$hangar_backup/api-ref.txt")
hangar_web_ref=$(cat "$hangar_backup/web-ref.txt")
docker tag "hangar-rollback-api:$hangar_stamp" "$hangar_api_ref"
docker tag "hangar-rollback-web:$hangar_stamp" "$hangar_web_ref"
"$COMPOSE" up -d --no-deps --no-build --pull never --force-recreate api web
git switch --detach "$(cat "$hangar_backup/source-before.txt")"
"$COMPOSE" ps
"$COMPOSE" logs --tail=100 api web
```

Новая функция не меняет схему и не пишет данные: обычный откат требует возврата
образов, **не** восстановления БД. Не восстанавливать dump поверх рабочей БД без
отдельной причины: это удалит изменения пользователей после резервной копии.
